from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache

from django_ical.views import ICalFeed

from registration.utils import get_or_404
from registration.models import Event, HelperShift
from registration.permissions import ACCESS_EVENT_VIEW_CALENDAR, has_access

from urllib.parse import urlencode
from hashlib import sha256

EVENT_FEED_SALT = "registration.event-calendar"


def event_feed_url(request, event):
    if not request.user.is_active or not has_access(request.user, event, ACCESS_EVENT_VIEW_CALENDAR):
        return None

    token = signing.dumps({"event": event.pk, "user": str(request.user.pk)}, salt=EVENT_FEED_SALT)
    path = reverse("event_ical", args=[event.url_name])
    return request.build_absolute_uri("{}?{}".format(path, urlencode({"token": token})))


class EventFeed(ICalFeed):
    timezone = settings.TIME_ZONE
    product_id = "-//helfertool.org//Helfertool"

    @method_decorator(never_cache)
    def __call__(self, request, *args, **kwargs):
        return super().__call__(request, *args, **kwargs)

    def get_object(self, request, event_url_name):
        event = get_object_or_404(Event, url_name=event_url_name)
        try:
            data = signing.loads(request.GET.get("token", ""), salt=EVENT_FEED_SALT)
        except signing.BadSignature as exc:
            raise PermissionDenied from exc

        if not isinstance(data, dict) or data.get("event") != event.pk or not isinstance(data.get("user"), str):
            raise PermissionDenied

        try:
            user = get_user_model().objects.filter(pk=data["user"], is_active=True).first()
        except (ValueError, ValidationError) as exc:
            raise PermissionDenied from exc
        if user is None or not has_access(user, event, ACCESS_EVENT_VIEW_CALENDAR):
            raise PermissionDenied

        return event

    def title(self, event):
        return event.name

    def link(self, event):
        return reverse("helpers", args=[event.url_name])

    def file_name(self, event):
        return "{}-helpers.ics".format(event.url_name)

    def items(self, event):
        return (
            HelperShift.objects.filter(helper__event=event, shift__job__event=event)
            .select_related("helper__event", "shift__job")
            .order_by("shift__begin", "shift__end", "shift_id", "helper_id")
        )

    def item_title(self, assignment):
        shift = assignment.shift
        parts = [shift.job.name]
        if shift.name:
            parts.append(shift.name)
        parts.append(assignment.helper.full_name)
        return " - ".join(parts)

    def item_description(self, assignment):
        return ""

    def item_link(self, assignment):
        return reverse("view_helper_assignment", args=[assignment.helper.event.url_name, assignment.pk])

    def item_start_datetime(self, assignment):
        return assignment.shift.begin

    def item_end_datetime(self, assignment):
        return assignment.shift.end

    def item_timestamp(self, assignment):
        return timezone.now()

    def item_guid(self, assignment):
        # Helper UUIDs are bearer credentials for public self-service pages.
        identity = "event-{}-helper-{}-shift-{}".format(
            assignment.helper.event_id, assignment.helper_id, assignment.shift_id
        )
        return "{}@helfertool".format(sha256(identity.encode()).hexdigest())


class HelperFeed(ICalFeed):
    timezone = settings.TIME_ZONE
    product_id = "-//helfertool.org//Helfertool"

    def get_object(self, request, event_url_name, helper_pk):
        event, job, shift, helper = get_or_404(event_url_name, helper_pk=helper_pk)
        self._event = event
        self._helper = helper
        self._registered_link = reverse("registered", args=[event.url_name, helper.pk])

        return helper

    def file_name(self, helper):
        return "{}.ics".format(helper.event.name)

    def items(self, helper):
        return helper.shifts.all()

    def item_title(self, shift):
        return "{} - {}".format(self._event.name, shift.job.name)

    def item_description(self, shift):
        return ""

    def item_link(self, shift):
        return self._registered_link

    def item_start_datetime(self, shift):
        return shift.begin

    def item_end_datetime(self, shift):
        return shift.end

    def item_guid(self, shift):
        return "{}#{}".format(self.item_link(shift), shift.pk)
