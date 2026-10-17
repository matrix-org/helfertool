from django.utils.translation import gettext as _
from django.db.models import Count, Q, Value
from django.db.models.functions import Coalesce
from django.core.exceptions import ValidationError
from django.utils import timezone

from ..models import Helper

import re
import xlsxwriter

# pylint: disable=E1102


class Iterator:
    """Returns ascending natural numbers beginning from 0."""

    def __init__(self):
        self.__v = -1

    def next(self):
        """Returns the next number beginning from 0."""
        self.__v += 1
        return self.__v

    def add(self, n):
        """Returns the next number beginning from 0."""
        self.__v += n
        return self.__v

    def get(self):
        """Returns the current number.

        get() should only be used after next() to get the same number again.
        """
        return self.__v

    def reset(self):
        """Resets the counter.

        The first call to next() after this returns 0."""
        self.__v = -1


def clean_name(name):
    """Cleans the name to be a valid sheet name in excel.

    The characters [ ] : * ? / \\ are removed.
    """
    return re.sub(r"[\[\]:*?\/]", "", name)


def escape(payload):
    if not payload:
        return ""

    # http://blog.zsec.uk/csv-dangers-mitigations/
    if payload[0] in ("@", "+", "-", "=", "|"):
        payload = payload.replace("|", r"\|")
        payload = "'" + payload + "'"
    return payload


def xlsx(buffer, event, jobs, date, include_sensitive):
    """Exports the helpers for given jobs of an event as excel spreadsheet.

    Parameter:
        buffer: a writeable bytes buffer (e.g. io.BytesIO or a file)
        event:  the exported event
        jobs:   a list of all exported jobs
        date:   export only for this date
        include_sensitive: Include sensitive data in export
    """
    # create xlsx
    workbook = xlsxwriter.Workbook(buffer)

    # duplicated worksheet names are not allowed
    used_names = []

    # export jobs
    for job in jobs:
        # find unique worksheet name
        job_name = clean_name(job.name)[:20]  # worksheet name must be <= 31 chars

        job_name_use = job_name
        counter = 2
        while job_name_use in used_names:
            job_name_use = "{}{}".format(job_name, counter)
            counter += 1

        used_names.append(job_name_use)

        # add things
        worksheet = workbook.add_worksheet(job_name_use)
        bold = workbook.add_format({"bold": True})
        multiple_shifts = workbook.add_format({"bg_color": "#fFFF99"})

        row = Iterator()
        column = Iterator()

        # header
        worksheet.write(0, column.next(), _("First name"), bold)
        worksheet.write(0, column.next(), _("Surname"), bold)
        worksheet.write(0, column.next(), _("E-Mail"), bold)
        worksheet.set_column(0, column.get(), 30)

        if event.ask_phone and include_sensitive:
            worksheet.write(0, column.next(), _("Mobile phone"), bold)
            worksheet.set_column(column.get(), column.get(), 20)

        if event.ask_shirt:
            worksheet.write(0, column.next(), _("T-shirt"), bold)
            worksheet.set_column(column.get(), column.get(), 10)

        if event.ask_nutrition:
            worksheet.write(0, column.next(), _("Nutrition"), bold)
            worksheet.set_column(column.get(), column.get(), 13)

        if job.infection_instruction:
            worksheet.write(0, column.next(), _("Food handling"), bold)
            worksheet.set_column(column.get(), column.get(), 20)

        worksheet.write(0, column.next(), _("Comment"), bold)
        worksheet.set_column(column.get(), column.get(), 50)

        # last column, needed for merge later
        last_column = column.get()

        # freeze header
        worksheet.freeze_panes(1, 0)

        # skip first row
        row.next()

        # coordinators
        if not date and job.coordinators.exists():
            worksheet.merge_range(row.next(), 0, row.get(), last_column, _("Coordinators"), bold)
            add_helpers(worksheet, row, column, event, job, job.coordinators.all(), multiple_shifts, include_sensitive)

        # show all shifts
        for shift in job.shift_set.order_by("begin"):
            if date and shift.date() != date:
                continue

            shift_text = shift.time()
            if shift.name != "":
                shift_text += f" ({shift.name})"
            worksheet.merge_range(row.next(), 0, row.get(), last_column, shift_text, bold)
            add_helpers(worksheet, row, column, event, job, shift.helper_set.all(), multiple_shifts, include_sensitive)

    # close xlsx
    workbook.close()


def helper_list_xlsx(buffer, event, include_sensitive, include_internal):
    """Export business helper fields once per person, without assignment details."""
    fields = [
        "firstname",
        "surname",
        "email",
        "mxid",
        "phone",
        "comment",
        "internal_comment",
        "shirt",
        "nutrition",
        "infection_instruction",
        "timestamp",
        "validated",
        "timestamp_validated",
        "mail_failed",
        "privacy_statement",
    ]
    if not include_sensitive:
        fields.remove("phone")
    if not include_internal:
        fields.remove("internal_comment")

    labels = {"timestamp_validated": _("Validation time"), "mail_failed": _("Mail delivery error")}
    headers = [str(labels.get(field, Helper._meta.get_field(field).verbose_name)) for field in fields]
    headers.extend([_("Registered shifts"), _("Coordinator assignments"), _("Buy a T-shirt for helper")])
    helpers = event.helper_set.annotate(
        registered_shifts=Count("shifts", filter=Q(shifts__job__event=event), distinct=True),
        coordinator_assignments=Count("job", filter=Q(job__event=event), distinct=True),
        buy_shirt=Coalesce("gifts__buy_shirt", Value(False)),
    ).order_by("surname", "firstname", "pk")

    with xlsxwriter.Workbook(buffer, {"strings_to_formulas": False, "strings_to_urls": False}) as workbook:
        worksheet = workbook.add_worksheet(clean_name(_("Helpers"))[:31])
        bold = workbook.add_format({"bold": True})
        date_format = workbook.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
        worksheet.write_row(0, 0, headers, bold)
        worksheet.set_column(0, len(headers) - 1, 24)
        worksheet.freeze_panes(1, 0)
        row_count = 0
        for row_count, helper in enumerate(helpers, start=1):
            for column, field in enumerate(fields):
                value = getattr(helper, field)
                if field in ["shirt", "nutrition", "infection_instruction"]:
                    value = getattr(helper, "get_{}_display".format(field))()
                if field in ["timestamp", "timestamp_validated"] and value is not None:
                    worksheet.write_datetime(
                        row_count, column, timezone.localtime(value).replace(tzinfo=None), date_format
                    )
                elif isinstance(value, str):
                    if len(value) > 32767:
                        raise ValidationError(
                            _("Cannot export: a helper field exceeds Excel's limit of 32,767 characters per cell.")
                        )
                    worksheet.write_string(row_count, column, value)
                else:
                    worksheet.write(row_count, column, value)
            worksheet.write_number(row_count, len(fields), helper.registered_shifts)
            worksheet.write_number(row_count, len(fields) + 1, helper.coordinator_assignments)
            worksheet.write_boolean(row_count, len(fields) + 2, helper.buy_shirt)
        worksheet.autofilter(0, 0, row_count, len(headers) - 1)


def add_helpers(worksheet, row, column, event, job, helpers, multiple_shifts_format, include_sensitive):
    for helper in helpers:
        row.next()
        column.reset()

        num_shifts = helper.shifts.count()
        num_jobs = len(helper.coordinated_jobs)
        cell_format = None
        if num_shifts + num_jobs > 1:
            cell_format = multiple_shifts_format

        worksheet.write(row.get(), column.next(), escape(helper.firstname), cell_format)
        worksheet.write(row.get(), column.next(), escape(helper.surname), cell_format)
        worksheet.write(row.get(), column.next(), escape(helper.email), cell_format)
        if event.ask_phone and include_sensitive:
            worksheet.write(row.get(), column.next(), escape(helper.phone), cell_format)
        if event.ask_shirt:
            worksheet.write(row.get(), column.next(), escape(str(helper.get_shirt_display())), cell_format)
        if event.ask_nutrition:
            worksheet.write(row.get(), column.next(), escape(str(helper.get_nutrition_short())), cell_format)
        if job.infection_instruction:
            worksheet.write(
                row.get(), column.next(), escape(str(helper.get_infection_instruction_short())), cell_format
            )
        worksheet.write(row.get(), column.next(), escape(helper.comment), cell_format)
