import datetime
import logging
from dateutil.tz import tzutc
from pkg_resources import parse_version

import newrelic.agent
from django.db import transaction
from django.db.models import Max
from django.db.utils import IntegrityError

from missioncontrol.celery import celery
from missioncontrol.base.models import (Application,
                                        Build,
                                        Channel,
                                        Datum,
                                        Measure,
                                        Platform)
from missioncontrol.settings import MISSION_CONTROL_TABLE
from .measuresummary import update_measure_summary
from .versions import get_major_version


logger = logging.getLogger(__name__)


@celery.task
def update_measures(application_name, platform_name, channel_name,
                    submission_date=None, bulk_create=True):
    '''
    Updates (or creates) a local cache entry for a specify platform/channel/measure
    aggregate, which can later be retrieved by the API
    '''
    # hack: importing raw_query here to make monkeypatching work
    # (if we put it on top it is impossible to override if something
    # else imports this module first)
    from .presto import raw_query

    logger.info('Updating measures: %s %s (date: %s)', channel_name, platform_name,
                submission_date or 'latest')

    newrelic.agent.add_custom_parameter("application", application_name)
    newrelic.agent.add_custom_parameter("platform", platform_name)
    newrelic.agent.add_custom_parameter("channel", channel_name)

    application = Application.objects.get(name=application_name)
    platform = Platform.objects.get(name=platform_name)
    channel = Channel.objects.get(name=channel_name)
    measures = Measure.objects.filter(channels=channel,
                                      application=application,
                                      platform=platform)
    if submission_date is None:
        now = datetime.datetime.utcnow()
        submission_date = datetime.datetime(year=now.year, month=now.month,
                                            day=now.day, tzinfo=tzutc())
        min_timestamp = Datum.objects.filter(
            timestamp__gte=submission_date,
            build__channel=channel,
            measure__in=measures).aggregate(Max('timestamp'))['timestamp__max']
    else:
        min_timestamp = None

    if not min_timestamp:
        min_timestamp = submission_date

    # ignore any buildids older than twice the update interval
    min_buildid_timestamp = submission_date - (channel.update_interval * 2)
    # ignore any buildids in the future of the submission date
    max_buildid_timestamp = submission_date + datetime.timedelta(days=1)

    # also place a restriction on version (to avoid fetching data
    # for bogus versions)
    valid_versions = sorted(
        list(
            Build.objects.filter(
                application=application,
                channel=channel,
                platform=platform,
                build_id__gte=min_buildid_timestamp.strftime('%Y%m%d'),
                build_id__lte=max_buildid_timestamp.strftime('%Y%m%d')).values_list(
                    'version', flat=True)
        ), key=parse_version)
    if not valid_versions:
        raise Exception('No valid versions found for combination: {}'.format(
            '/'.join(('application', 'channel', 'platform'))))
    (min_version, max_version) = (get_major_version(valid_versions[0]),
                                  get_major_version(valid_versions[-1]) + 1)

    # we prefer to specify parameters in a seperate params dictionary
    # where possible (to reduce the risk of creating a malformed
    # query from incorrect parameters
    measure_sums = ', '.join([
        'sum({})'.format(measure.name) for measure in measures])
    query_template = f'''
        select window_start, build_id, display_version, sum(usage_hours),
        sum(count),
        {measure_sums}
        from {MISSION_CONTROL_TABLE} where
        application=%(application_name)s and
        display_version > %(min_version)s and display_version < %(max_version)s and
        build_id > %(min_build_id)s and build_id < %(max_build_id)s and
        os_name=%(os_name)s and
        channel=%(channel_name)s and
        window_start > timestamp %(min_timestamp)s and
        submission_date_s3 = %(submission_date)s
        group by (window_start, build_id, display_version)
        having sum(usage_hours) > 0'''.replace('\n', '').strip()
    params = {
        'application_name': application.telemetry_name,
        'min_version': str(min_version),
        'max_version': str(max_version),
        'min_build_id': min_buildid_timestamp.strftime('%Y%m%d'),
        'max_build_id': max_buildid_timestamp.strftime('%Y%m%d'),
        'os_name': platform.telemetry_name,
        'channel_name': channel_name,
        'min_timestamp': min_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        'submission_date': submission_date.strftime("%Y%m%d")
    }
    logger.info('Querying: %s', query_template % params)

    # bulk create any new datum objects from the returned results
    build_cache = {}
    datum_objs = []
    for row in raw_query(query_template, params):
        (window_start, build_id, version, usage_hours, client_count) = row[:5]
        for (measure, measure_count) in zip(measures, row[5:]):
            if measure_count is None:
                measure_count = 0
            # skip datapoints with negative measure counts or no usage hours
            # (in theory negative measures should be rejected at the ping
            # validation level, but this is not yet the case at the time of this
            # writing -- https://bugzilla.mozilla.org/show_bug.cgi?id=1447038)
            if measure_count < 0 or usage_hours <= 0:
                continue
            build = build_cache.get((build_id, version))
            if not build:
                try:
                    build = Build.objects.get(
                        platform=platform, channel=channel, build_id=build_id,
                        version=version)
                    build_cache[(build_id, version)] = build
                except Build.DoesNotExist:
                    # build not released by us, skip
                    continue
            # presto doesn't specify timezone information (but it's really utc)
            window_start = datetime.datetime.fromtimestamp(
                window_start.timestamp(), tz=tzutc())
            datum_objs.append(Datum(
                build=build,
                measure=measure,
                timestamp=window_start,
                value=measure_count,
                usage_hours=usage_hours,
                client_count=client_count))
    if bulk_create:
        Datum.objects.bulk_create(datum_objs)
    else:
        for datum_obj in datum_objs:
            try:
                with transaction.atomic():
                    datum_obj.save()
            except IntegrityError:
                continue

    # update the measure summary in our cache
    for measure in measures:
        update_measure_summary.apply_async(
            args=[application_name, platform_name, channel_name,
                  measure.name])
