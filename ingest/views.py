import os
import logging
import io
from datetime import datetime, timezone
import json  # TODO consider faster APIs

from django.shortcuts import get_object_or_404
from django.db.models import Max
from django.views import View
from django.core import exceptions
from django.core.exceptions import ValidationError
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from compat.auth import parse_auth_header_value
from compat.dsn import get_sentry_key

from projects.models import Project
from issues.models import Issue, IssueStateManager, Grouping, TurningPoint, TurningPointKind
from issues.utils import get_type_and_value_for_data, get_issue_grouper_for_data, get_denormalized_fields_for_data
from issues.regressions import issue_is_regression

from bugsink.transaction import immediate_atomic, delay_on_commit
from bugsink.exceptions import ViolatedExpectation
from bugsink.streams import content_encoding_reader, MaxDataReader, MaxDataWriter, NullWriter, MaxLengthExceeded
from bugsink.app_settings import get_settings

from events.models import Event
from events.retention import evict_for_max_events, should_evict
from releases.models import create_release_if_needed
from alerts.tasks import send_new_issue_alert, send_regression_alert
from compat.timestamp import format_timestamp, parse_timestamp

from .parsers import StreamingEnvelopeParser, ParseError
from .filestore import get_filename_for_event_id
from .tasks import digest
from .event_counter import check_for_thresholds


HTTP_429_TOO_MANY_REQUESTS = 429
HTTP_400_BAD_REQUEST = 400
HTTP_501_NOT_IMPLEMENTED = 501


logger = logging.getLogger("bugsink.ingest")
performance_logger = logging.getLogger("bugsink.performance.ingest")


@method_decorator(csrf_exempt, name='dispatch')
class BaseIngestAPIView(View):

    def post(self, request, project_pk=None):
        try:
            return self._post(request, project_pk)
        except MaxLengthExceeded as e:
            return JsonResponse({"message": str(e)}, status=HTTP_400_BAD_REQUEST)
        except exceptions.ValidationError as e:
            return JsonResponse({"message": str(e)}, status=HTTP_400_BAD_REQUEST)

    @classmethod
    def get_sentry_key_for_request(cls, request):
        # we simply pick the first authentication mechanism that matches, rather than raising a SuspiciousOperation as
        # sentry does (I found the supplied reasons unconvincing). See https://github.com/getsentry/relay/pull/602

        # "In situations where it's not possible to send [..] header, it's possible [..] values via the querystring"
        # https://github.com/getsentry/develop/blob/b24a602de05b/src/docs/sdk/overview.mdx#L171
        if "sentry_key" in request.GET:
            return request.GET["sentry_key"]

        # Sentry used to support HTTP_AUTHORIZATION too, but that is unused since Sept. 27 2011
        if "HTTP_X_SENTRY_AUTH" in request.META:
            auth_dict = parse_auth_header_value(request.META["HTTP_X_SENTRY_AUTH"])
            return auth_dict.get("sentry_key")

        raise exceptions.NotAuthenticated("Unable to find authentication information")

    @classmethod
    def get_project(cls, project_pk, sentry_key):
        # NOTE this gives a 404 for non-properly authorized. Is this really something we care about, i.e. do we want to
        # raise NotAuthenticated? In that case we need to get the project first, and then do a constant-time-comp on the
        # sentry_key
        return get_object_or_404(Project, pk=project_pk, sentry_key=sentry_key)

    @classmethod
    def get_project_for_request(cls, project_pk, request):
        sentry_key = cls.get_sentry_key_for_request(request)
        return cls.get_project(project_pk, sentry_key)

    @classmethod
    def process_event(cls, event_id, event_data_stream, project, request):
        # because we want to count events before having created event objects (quota may block the latter) we cannot
        # depend on event.timestamp; instead, we look on the clock once here, and then use that everywhere.
        now = datetime.now(timezone.utc)

        event_metadata = cls.get_event_meta(now, request, project)

        if get_settings().DIGEST_IMMEDIATELY:
            # in this case the stream will be an BytesIO object, so we can actually call .get_value() on it.
            event_data_bytes = event_data_stream.getvalue()
            event_data = json.loads(event_data_bytes.decode("utf-8"))
            performance_logger.info("ingested event with %s bytes", len(event_data_bytes))
            cls.digest_event(event_metadata, event_data, project=project)
        else:
            # In this case the stream will be a file that has been written the event's content to it.
            # To ensure that the (possibly EAGER) handling of the digest has the file available, we flush it here:
            event_data_stream.flush()

            performance_logger.info("ingested event with %s bytes", event_data_stream.bytes_written)
            digest.delay(event_id, event_metadata)

    @classmethod
    def get_event_meta(cls, now, request, project):
        debug_info = request.META.get("HTTP_X_BUGSINK_DEBUGINFO", "")
        return {
            "project_id": project.id,
            "timestamp": format_timestamp(now),
            "debug_info": debug_info,
        }

    @classmethod
    @immediate_atomic()
    def digest_event(cls, event_metadata, event_data, project=None):
        if project is None:
            # having project as an optional argument allows us to pass this in when we have the information available
            # (in the DIGEST_IMMEDIATELY case) which saves us a query.
            project = Project.objects.get(pk=event_metadata["project_id"])

        timestamp = parse_timestamp(event_metadata["timestamp"])

        if not cls.count_project_periods_and_act_on_it(project, timestamp):
            return  # if over-quota: just return (any cleanup is done calling-side)

        # I resisted the temptation to put `get_denormalized_fields_for_data` in an if-statement: you basically "always"
        # need this info... except when duplicate event-ids are sent. But the latter is the exception, and putting this
        # in an if-statement would require more rework (and possibly extra queries) than it's worth.
        denormalized_fields = get_denormalized_fields_for_data(event_data)
        # the 3 lines below are suggestive of a further inlining of the get_type_and_value_for_data function
        calculated_type, calculated_value = get_type_and_value_for_data(event_data)
        denormalized_fields["calculated_type"] = calculated_type
        denormalized_fields["calculated_value"] = calculated_value

        grouping_key = get_issue_grouper_for_data(event_data, calculated_type, calculated_value)

        if not Grouping.objects.filter(project_id=event_metadata["project_id"], grouping_key=grouping_key).exists():
            # we don't have Project.issue_count here ('premature optimization') so we just do an aggregate instead.
            max_current = Issue.objects.filter(project_id=event_metadata["project_id"]).aggregate(
                Max("digest_order"))["digest_order__max"]
            issue_digest_order = max_current + 1 if max_current is not None else 1

            issue = Issue.objects.create(
                digest_order=issue_digest_order,
                project_id=event_metadata["project_id"],
                first_seen=timestamp,
                last_seen=timestamp,
                digested_event_count=1,
                **denormalized_fields,
            )
            # even though in our data-model a given grouping does not imply a single Issue (in fact, that's the whole
            # point of groupings as a data-model), at-creation such implication does exist, because manual information
            # ("this grouper is actually part of some other issue") can by definition not yet have been specified.
            issue_created = True

            grouping = Grouping.objects.create(
                project_id=event_metadata["project_id"],
                grouping_key=grouping_key,
                issue=issue,
            )

        else:
            grouping = Grouping.objects.get(project_id=event_metadata["project_id"], grouping_key=grouping_key)
            issue = grouping.issue
            issue_created = False

            # update the denormalized fields
            issue.last_seen = timestamp
            issue.digested_event_count += 1

        # NOTE: possibly expensive. "in theory" we can just do some bookkeeping for a denormalized value, but that may
        # be hard to keep in-sync in practice. Let's check the actual cost first.
        # +1 because we're about to add one event.
        project_stored_event_count = (project.event_set.count() or 0) + 1
        issue_stored_event_count = (issue.event_set.count() or 0) + 1

        if should_evict(project, timestamp, project_stored_event_count):
            # Note: I considered pushing this into some async process, but it makes reasoning much harder, and it's
            # doubtful whether it would help, because in the end there's just a single pipeline of ingested-related
            # stuff todo, might as well do the work straight away. Similar thoughts about pushing this into something
            # cron-like. (not exactly the same, because for cron-like time savings are possible if the cron-likeness
            # causes the work to be outside of the 'rush hour' -- OTOH this also introduces a lot of complexity about
            # "what is a limit anyway, if you can go either over it, or work is done before the limit is reached")
            evict_for_max_events(project, timestamp, project_stored_event_count)

        # NOTE: an event always has a single (automatically calculated) Grouping associated with it. Since we have that
        # information available here, we could add it to the Event model.
        event, event_created = Event.from_ingested(
            event_metadata,
            issue.digested_event_count,
            issue_stored_event_count,
            issue,
            event_data,
            denormalized_fields,
        )
        if not event_created:
            if issue_created:
                # this is a weird case that "should not happen" (but can happen in practice). Namely, when some client
                # sends events with the same event_id (leading to no new event), but different-enough actual data that
                # they lead to new issue-creation. I've already run into this while debugging (and this may in fact be
                # the only realistic scenario): manually editing some sample event but not updating the event_id (nor
                # running send_json with --fresh-id). We raise an exception after cleaning up, to at least avoid getting
                # into an inconsistent state in the DB.
                issue.delete()
                raise ViolatedExpectation("no event created, but issue created")

            # Validating by letting the DB raise an exception, and only after taking some other actions already, is not
            # "by the book" (some book), but it's the most efficient way of doing it when your basic expectation is that
            # multiple events with the same event_id "don't happen" (i.e. are the result of badly misbehaving clients)
            raise ValidationError("Event already exists", code="event_already_exists")

        release = create_release_if_needed(project, event.release, event)

        if issue_created:
            TurningPoint.objects.create(
                issue=issue, triggering_event=event, timestamp=timestamp,
                kind=TurningPointKind.FIRST_SEEN)
            event.never_evict = True

            if project.alert_on_new_issue:
                delay_on_commit(send_new_issue_alert, str(issue.id))

        else:
            # new issues cannot be regressions by definition, hence this is in the 'else' branch
            if issue_is_regression(issue, event.release):
                TurningPoint.objects.create(
                    issue=issue, triggering_event=event, timestamp=timestamp,
                    kind=TurningPointKind.REGRESSED)
                event.never_evict = True

                if project.alert_on_regression:
                    delay_on_commit(send_regression_alert, str(issue.id))

                IssueStateManager.reopen(issue)

            # note that while digesting we _only_ care about .is_muted to determine whether unmuting (and alerting about
            # unmuting) should happen, whether as a result of a VBC or 'after time'. Somewhat counter-intuitively, a
            # 'muted' issue is thus not treated as something to more deeply ignore than an unresolved issue (and in
            # fact, conversely, it may be more loud when the for/until condition runs out). This is in fact analogous to
            # "resolved" issues which are _also_ treated with more "suspicion" than their unresolved counterparts.
            if issue.is_muted and issue.unmute_after is not None and timestamp > issue.unmute_after:
                # note that unmuting on-ingest implies that issues that no longer occur stay muted. I'd say this is what
                # you want: things that no longer happen should _not_ draw your attention, and if you've nicely moved
                # some issue away from the "Open" tab it should not reappear there if a certain amount of time passes.
                # Thus, unmute_after should more completely be called unmute_until_events_happen_after but that's a bit
                # long. Phrased slightly differently: you basically click the button saying "I suppose this issue will
                # self-resolve in x time; notify me if this is not the case"
                IssueStateManager.unmute(
                    issue, triggering_event=event,
                    unmute_metadata={"mute_for": {"unmute_after": issue.unmute_after}})

        if event.never_evict:
            # as a sort of poor man's django-dirtyfields (which we haven't adopted for simplicity's sake) we simply do
            # this manually for a single field; we know that if never_evict has been set, it's always been set after the
            # .create call, i.e. its results still need to be saved. We accept the cost of the extra .save call, since
            # TurningPoints are relatively rare (and hence so is this setting of `never_evict` and the associated save
            # call)
            event.save()

        if release.version + "\n" not in issue.events_at:
            issue.events_at += release.version + "\n"

        cls.count_issue_periods_and_act_on_it(issue, event, timestamp)

        issue.save()

    @classmethod
    def count_project_periods_and_act_on_it(cls, project, now):
        # returns: True if any further processing should be done.

        thresholds = [
            ("minute", 5, get_settings().MAX_EVENTS_PER_PROJECT_PER_5_MINUTES, None),
            ("minute", 60, get_settings().MAX_EVENTS_PER_PROJECT_PER_HOUR, None),
        ]

        if project.quota_exceeded_until is not None and now < project.quota_exceeded_until:
            # This is the same check that we do on-ingest. Naively, one might think that it is superfluous. However,
            # because by design there is the potential for a digestion backlog to exist, it is possible that the update
            # of `project.quota_exceeded_until` happens only after any number of events have already passed through
            # ingestion and have entered the pipeline. Hence we double-check on-digest.

            # nothing to check (otherwise) or update on project in this case, also abort further event-processing
            return False

        project.digested_event_count += 1

        if project.digested_event_count >= project.next_quota_check:
            # check_for_thresholds is relatively expensive (SQL group by); we do it as little as possible

            # Notes on off-by-one:
            # * check_for_thresholds tests for "gte"; this means it will trigger once the quota is reached (not
            #   exceeded). We act accordingly by accepting this final event (returning True, count += 1) and closing the
            #   door (setting quota_exceeded_until).
            # * add_for_current=1 because we're called before the event is digested (it's not in Event.objects.filter),
            #   and because of the previous bullet we know that it will always be digested.
            states = check_for_thresholds(Event.objects.filter(project=project), now, thresholds, 1)

            until = max([below_from for (state, below_from, _, _) in states if state], default=None)

            # "at least 1" is a matter of taste; because the actual usage of `next_quota_check` is with a gte, leaving
            # it out would result in the same behavior. But once we reach this point we know that digested_event_count
            # will increase, so we know that a next check cannot happen before current + 1, we might as well be explicit
            check_again_after = max(1, min([check_after for (_, _, check_after, _) in states], default=1))

            project.quota_exceeded_until = until

            # note on correction of `digested_event_count += 1`: as long as we don't do that between the check on
            # next_quota_check (the if-statement) and the setting (the statement below) we're good.
            project.next_quota_check = project.digested_event_count + check_again_after

        project.save()
        return True

    @classmethod
    def count_issue_periods_and_act_on_it(cls, issue, event, timestamp):
        # See the project-version for various off-by-one notes (not reproduced here).
        #
        # We just have "unmute" as a purpose here, not "quota". I thought I'd have per-issue quota earlier (which would
        # ensure some kind of fairness within a project) but:
        #
        # * that doesn't quite work, because to determine the issue, you'd have to incur almost all of the digest cost.
        # * quota are expected to be set "high enough" anyway, i.e. only as a last line of defense against run-away
        #     clients
        # * "even if" you'd get this to work there'd be scenarios where it's useless, e.g. misbehaving groupers.
        thresholds = IssueStateManager.get_unmute_thresholds(issue)

        if thresholds and issue.digested_event_count >= issue.next_unmute_check:
            states = check_for_thresholds(Event.objects.filter(issue=issue), timestamp, thresholds)

            check_again_after = max(1, min([check_after for (_, _, check_after, _) in states], default=1))

            issue.next_unmute_check = issue.digested_event_count + check_again_after

            for (state, until, _, vbc_dict) in states:
                if not state:
                    continue

                IssueStateManager.unmute(issue, triggering_event=event, unmute_metadata={"mute_until": vbc_dict})

                # In the (in the current UI impossible, and generally unlikely) case that multiple unmute conditions are
                # met simultaneously, we arbitrarily break after the first. (this makes it so that a single TurningPoint
                # is created and that the detail that there was also another reason to unmute doesn't show us, but
                # that's perfectly fine); it also matches what we do elsewhere (i.e. `IssueStateManager.unmute` where we
                # have `if is_muted`)
                break


class IngestEventAPIView(BaseIngestAPIView):

    def _post(self, request, project_pk=None):
        project = self.get_project_for_request(project_pk, request)

        # This endpoint is deprecated. Personally, I think it's the simpler (and given my goals therefore better) of the
        # two, but fighting windmills and all... given that it's deprecated, I'm not going to give it quite as much love
        # (at least for now). In particular, "not DIGEST_IMMEDIATELY" is not implemented here. Interfaces between the
        # internal methods quite changed a bit recently, and this one did not keep up. In particular: in our current set
        # of interfaces we need an event_id before parsing (or after parsing but then we have double work). Easiest
        # solution: just copy/paste from process_event(), and take only one branch.
        now = datetime.now(timezone.utc)

        event_metadata = self.get_event_meta(now, request, project)

        # if get_settings().DIGEST_IMMEDIATELY:  this is the only branch we implemented here.
        event_data = json.loads(
            MaxDataReader("MAX_EVENT_SIZE", content_encoding_reader(
                MaxDataReader("MAX_EVENT_COMPRESSED_SIZE", request))).read())

        self.digest_event(event_metadata, event_data, project=project)

        return HttpResponse()


class IngestEnvelopeAPIView(BaseIngestAPIView):

    def _post(self, request, project_pk=None):
        # Note: wrapping the COMPRESSES_SIZE checks arount request makes it so that when clients do not compress their
        # requests, they are still subject to the (smaller) maximums that apply pre-uncompress. This is exactly what we
        # want.
        now = datetime.now(timezone.utc)

        parser = StreamingEnvelopeParser(
                    MaxDataReader("MAX_ENVELOPE_SIZE", content_encoding_reader(
                        MaxDataReader("MAX_ENVELOPE_COMPRESSED_SIZE", request))))

        envelope_headers = parser.get_envelope_headers()
        if "dsn" in envelope_headers:
            project = self.get_project(project_pk, get_sentry_key(envelope_headers["dsn"]))
        else:
            project = self.get_project_for_request(project_pk, request)

        if project.quota_exceeded_until is not None and now < project.quota_exceeded_until:
            # Sentry has x-sentry-rate-limits, but for now 429 is just fine. Client-side this is implemented as a 60s
            # backoff.
            #
            # Note "what's the use of this?": in my actual setups I have observed that we're almost entirely limited by
            # (nginx's) SSL processing on-ingest, and that digest is (almost) able to keep up. Because of request
            # buffering, the cost of such processing will already have been incurred once you reach this point. So is
            # the entire idea of quota useless? No, because the SDK will generally back off on 429, this does create
            # some relief. Also: avoid backlogging the digestion pipeline.
            #
            # Another aspect is: the quota serve as a first threshold for retention/evictions, i.e. some quota will mean
            # that retention is not too heavily favoring "most recent" when there are very many requests coming in.
            #
            # Note that events that exceed the quota will not be seen (not even counted) in any way.
            return HttpResponse(status=HTTP_429_TOO_MANY_REQUESTS)

        def factory(item_headers):
            if item_headers.get("type") == "event":
                if get_settings().DIGEST_IMMEDIATELY:
                    return MaxDataWriter("MAX_EVENT_SIZE", io.BytesIO())

                # envelope_headers["event_id"] is required when type=event per the spec (and takes precedence over the
                # payload's event_id), so we can rely on it having been set.
                if "event_id" not in envelope_headers:
                    raise ParseError("event_id not found in envelope headers")
                filename = get_filename_for_event_id(envelope_headers["event_id"])
                os.makedirs(os.path.dirname(filename), exist_ok=True)
                return MaxDataWriter("MAX_EVENT_SIZE", open(filename, 'wb'))

            # everything else can be discarded; (we don't check for individual size limits, because these differ
            # per item type, we have the envelope limit to protect us, and we incur almost no cost (NullWriter) anyway.
            return NullWriter()

        for item_headers, event_output_stream in parser.get_items(factory):
            try:
                if item_headers.get("type") != "event":
                    logger.info("skipping non-event item: %s", item_headers.get("type"))

                    if item_headers.get("type") == "transaction":
                        # From the spec of type=event: This Item is mutually exclusive with `"transaction"` Items.
                        # i.e. when we see a transaction, a regular event will not be present and we can stop.
                        logger.info("discarding the rest of the envelope")
                        break

                    continue

                self.process_event(envelope_headers["event_id"], event_output_stream, project, request)
                break  # From the spec of type=event: This Item may occur at most once per Envelope. i.e. seen=done

            finally:
                event_output_stream.close()

        return HttpResponse()


# Just a few thoughts on the relative performance of the main building blocks of dealing with Envelopes, and how this
# affect our decision making. On my local laptop (local loopback, django dev server), with a single 50KiB event, I
# measured the following approximate timtings:
# 0.00001s just get the bytes
# 0.0002s get bytes & gunzip
# 0.0005s get bytes, gunzip & json load
#
# The goal of having an ingest/digest split is to keep the server responsive in times of peek load. It is _not_ to have
# get crazy good throughput numbers (because ingest isn't the bottleneck anyway).
#
# One consideration was: should we just store the compressed envelope on-upload. Answer, given the numbers above: not
# needed, fine to unpack. This is also prudent from the perspective of storage, because the envelope may contain much
# more stuff that we don't care about (up to 20MiB compressed) whereas the max event size (uncompressed) is 1MiB.
# Another advantage: this allows us to raise the relevant Header parsing and size limitation Exceptions to the SDKs.
#
