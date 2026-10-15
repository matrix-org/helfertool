from django import forms
from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.template import Context, Engine, TemplateSyntaxError
from django.urls import reverse
from django.utils.timezone import localtime
from django.utils.translation import gettext_lazy as _

from django_select2.forms import Select2MultipleWidget
from smtplib import SMTPException

from .models import SentMail, MailDelivery
from .tracking import new_tracking_event

from registration.permissions import has_access, ACCESS_MAILS_SEND
from registration.models import HelperShift


class MailFormError(Exception):
    """Error during mail sending. The message will be displayed to the user, so do not include sensitive data."""

    pass


class MailForm(forms.Form):
    def __init__(self, *args, **kwargs):
        # get parameters
        self.event = kwargs.pop("event")
        self.user = kwargs.pop("user")

        # different select values

        # send to helpers and coordinators
        self.jobs_all = {}

        # only coordinators
        self.jobs_coordinators = {}

        # single shifts
        self.shifts = {}

        super(MailForm, self).__init__(*args, **kwargs)

        # get all allowed jobs
        choices = []

        # check if we can send mails to all helpers
        if has_access(self.user, self.event, ACCESS_MAILS_SEND):
            tmp = []
            tmp.append(("all", _("All helpers and coordinators")))
            tmp.append(("all-coords", _("All coordinators")))

            choices.append((_("General"), tmp))

        for job in self.event.job_set.all():
            if has_access(self.user, job, ACCESS_MAILS_SEND):
                tmp = []

                # helpers and coordinators
                title = _("{}, Helpers and coordinators").format(job.name)
                tmp.append(("job_%d_all" % job.pk, title))
                self.jobs_all["job_%d_all" % job.pk] = job

                # only coordinators
                title = _("{}, Coordinators").format(job.name)
                tmp.append(("job_%d_coords" % job.pk, title))
                self.jobs_coordinators["job_%d_coords" % job.pk] = job

                # shifts
                for shift in job.shift_set.all():
                    tmp.append(("shift_%d" % shift.pk, str(shift)))
                    self.shifts["shift_%d" % shift.pk] = shift

                choices.append((job.name, tmp))

        # reply to
        reply_to = []

        if has_access(self.user, self.event, ACCESS_MAILS_SEND):
            reply_to.append((self.event.email, self.event.email))

        reply_to.append((self.user.email, self.user.email))
        reply_to.append(("-", _("Custom")))

        # fields
        self.fields["receiver"] = forms.MultipleChoiceField(
            choices=choices,
            label=_("Receivers"),
            widget=Select2MultipleWidget,
        )

        self.fields["reply_to"] = forms.ChoiceField(
            choices=reply_to,
            label=_("Reply to"),
        )
        self.fields["reply_to"].widget.attrs["onChange"] = "handle_reply_to()"

        self.fields["custom_reply_to"] = forms.EmailField(
            label=_("Custom reply to"),
            help_text=_('Only used if "Custom" is selected above.'),
            required=False,
        )

        self.fields["cc"] = forms.EmailField(
            label=_("CC"),
            required=False,
        )

        self.fields["subject"] = forms.CharField(
            label=_("Subject"),
            max_length=200,
            help_text=_("Django template variables and filters are supported, for example {{ helper_name }}."),
        )

        self.fields["text"] = forms.CharField(
            widget=forms.Textarea,
            label=_("Text"),
            help_text=_(
                "Available variables: {{ helper_name }}, {{ helper_url }}, and helper_shifts "
                "(all assigned shifts, ordered chronologically). Each shift has job, name, begin, and end fields. "
                'Example: {% for shift in helper_shifts %}{{ shift.job }}: {{ shift.begin|date:"d.m.Y H:i" }}'
                "{% endfor %}. Reply-to and CC receive a copy for every helper, including their personal link."
            ),
        )

    def clean(self):
        cleaned_data = super(MailForm, self).clean()

        engine = Engine(loaders=[], autoescape=False)
        self.mail_templates = {}
        self.personalized = False
        for field in ("subject", "text"):
            source = cleaned_data.get(field)
            if source is not None:
                self.personalized |= any(marker in source for marker in ("{{", "{%", "{#"))
                try:
                    self.mail_templates[field] = engine.from_string(source)
                except TemplateSyntaxError as error:
                    self.add_error(field, _("Invalid mail template: %(error)s") % {"error": str(error)})

        if cleaned_data.get("reply_to") == "-" and not cleaned_data.get("custom_reply_to"):
            raise forms.ValidationError(_("You must specify a custom reply to address."))

        # check if there are recipients (will be checked again in send_mail, but we can provide better feedback here)
        if not self._get_helpers():
            raise forms.ValidationError(_("There are no helpers or coordinators that would receive this mail."))

    def send_mail(self, request):
        # basic parameters
        subject = self.cleaned_data.get("subject")
        text = self.cleaned_data.get("text")

        reply_to = self.cleaned_data.get("reply_to")
        if reply_to == "-":
            reply_to = self.cleaned_data.get("custom_reply_to")

        # model for log
        sentmail = SentMail.objects.create(
            event=self.event,
            user=self.user,
            sender=settings.EMAIL_SENDER_ADDRESS,
            subject=subject,
            text=text,
            reply_to=reply_to,
        )

        # CC
        cc = []
        if self.cleaned_data.get("cc"):
            cc = [
                self.cleaned_data.get("cc"),
            ]
            sentmail.cc = self.cleaned_data.get("cc")

        # tracking id
        tracking_uuid, tracking_header = new_tracking_event()
        sentmail.tracking_uuid = tracking_uuid

        # get the helpers that should receive the mail
        helpers = list({helper.pk: helper for helper in self._get_helpers(sentmail)}.values())

        # create mail delivery entry for tracking and a unique list of mail addresses
        receiver_list = []
        for h in helpers:
            MailDelivery.objects.create(helper=h, sentmail=sentmail)

            if h.email not in receiver_list:
                receiver_list.append(h.email)

        # save changed cc, tracking_uuid and things done in _get_helpers
        sentmail.save()

        if not receiver_list:
            sentmail.failed = True
            sentmail.save()
            raise MailFormError(_("There are no helpers or coordinators that would receive this mail."))

        try:
            messages = []
            if self.personalized:
                shifts = {helper.pk: [] for helper in helpers}
                assignments = (
                    HelperShift.objects.filter(helper_id__in=shifts)
                    .select_related("shift__job")
                    .order_by("shift__begin", "shift__end", "shift_id")
                )
                for assignment in assignments:
                    shift = assignment.shift
                    shifts[assignment.helper_id].append(
                        {
                            "job": shift.job.name,
                            "name": shift.name,
                            "begin": localtime(shift.begin),
                            "end": localtime(shift.end),
                        }
                    )
                for helper in helpers:
                    context = Context(
                        {
                            "helper_name": helper.full_name,
                            "helper_url": request.build_absolute_uri(
                                reverse("registered", args=[self.event.url_name, helper.pk])
                            ),
                            "helper_shifts": shifts[helper.pk],
                        },
                        autoescape=False,
                    )
                    messages.append(
                        (
                            self.mail_templates["subject"].render(context),
                            self.mail_templates["text"].render(context),
                            [helper.email],
                        )
                    )
            else:
                messages.append((subject, text, receiver_list))

            # Validate every rendered subject before sending any part of the campaign.
            if any("\n" in mail_subject or "\r" in mail_subject for mail_subject, _, _ in messages):
                raise MailFormError(_("Mail subjects must not contain line breaks."))
        except MailFormError:
            sentmail.failed = True
            sentmail.save()
            raise
        except Exception as error:
            sentmail.failed = True
            sentmail.save()
            raise MailFormError(
                _("Could not render the mail template. Please check its variables and filters.")
            ) from error

        try:
            with get_connection(fail_silently=False) as connection:
                for mail_subject, mail_text, receivers in messages:
                    if self.personalized:
                        mail_to = receivers
                    else:
                        mail_to = [reply_to]
                    mail = EmailMessage(
                        mail_subject,
                        mail_text,
                        settings.EMAIL_SENDER_ADDRESS,
                        mail_to,
                        receivers,
                        reply_to=[reply_to],
                        cc=cc,
                        headers=tracking_header,
                        connection=connection,
                    )
                    mail.send(fail_silently=False)
        except (SMTPException, OSError):
            sentmail.failed = True
            sentmail.save()
            raise

    def _get_helpers(self, sentmail=None):
        """Get the helpers that would receive the mail.

        If `sentmail` if provided the `SentMail` object is updated to contain this info."""
        receiver_list = self.cleaned_data.get("receiver")

        tmp = []

        for receiver in receiver_list:
            # all helpers and coordinators - special case: we can abort now
            if receiver == "all":
                if sentmail:
                    sentmail.all_helpers_and_coordinators = True
                return self.event.helper_set.distinct()
            # all coordinators
            elif receiver == "all-coords":
                tmp.extend(self.event.all_coordinators)
                if sentmail:
                    sentmail.all_coordinators = True
            # helpers and coordinators for job
            elif receiver in self.jobs_all:
                job_obj = self.jobs_all[receiver]
                tmp.extend(job_obj.helpers_and_coordinators())
                if sentmail:
                    sentmail.jobs_all.add(job_obj)
            # only coordinators for job
            elif receiver in self.jobs_coordinators:
                job_obj = self.jobs_coordinators[receiver]
                tmp.extend(job_obj.coordinators.all())
                if sentmail:
                    sentmail.jobs_only_coordinators.add(job_obj)
            # single shifts
            elif receiver in self.shifts:
                shift_obj = self.shifts[receiver]
                tmp.extend(shift_obj.helper_set.all())
                if sentmail:
                    sentmail.shifts.add(shift_obj)

        return tmp
