# coding: utf-8

import logging

from collections import defaultdict, OrderedDict
from decimal import Decimal
from itertools import tee

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models, transaction
from django.db.models import F, Sum
from django.utils.timezone import now

from fleetcore.fields import (
    MinuteField,
    MoneyField,
    SMSField,
    TaxField,
)
from fleetcore import pdf2cell
from fleetcore.pdf2cell import (
    EXCEEDED_MIN,
    EXCEEDED_MIN_PRICE,
    EXCEEDED_STABLISHING_MIN,
    EXCEEDED_STABLISHING_MIN_PRICE,
    IDL_MIN,
    IDL_PRICE,
    INCLUDED_MIN,
    MONTHLY_PRICE,
    NDL_MIN,
    NDL_PRICE,
    OTHER_PRICE,
    PHONE_NUMBER,
    PLAN,
    REFUNDS,
    SERVICES,
    SMS,
    SMS_PRICE,
    TOTAL_PRICE,
    USER,
)


def pairwise(iterable):
    "s -> (s0,s1), (s1,s2), (s2, s3), ..."
    a, b = tee(iterable)
    next(b, None)
    return zip(a, b)


class FleetUser(AbstractUser):
    leader = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name='leadering', null=True,
        on_delete=models.CASCADE)

    def __str__(self):
        leader = self.leader
        if self.leader is not None:
            leader = self.leader.get_full_name() or self.leader
        if leader:
            leader = ' (leadered by %s)' % leader
        return '%s - %s%s' % (self.username, self.get_full_name(), leader)


class LeaderTriangle(object):

    def __init__(self, leader):
        users = list(FleetUser.objects.filter(leader=leader)) + [leader]
        self.consumptions = self.consumption_set.filter(phone__user__in=users)
        if self.consumptions:
            totals = self.consumptions.aggregate(total=Sum('total'))
            self.total = totals['total']


class Fleet(models.Model):
    """Phones fleet."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    account_number = models.CharField(max_length=128)
    email = models.EmailField()
    provider = models.CharField(max_length=256)
    report_consumption_template = models.TextField(blank=True)

    def __str__(self):
        return '%s - %s' % (self.provider, self.account_number)


class Bill(models.Model):
    """Monthly bill for a fleet."""

    fleet = models.ForeignKey(Fleet, on_delete=models.CASCADE)
    invoice_filename = models.CharField(max_length=512, null=True, blank=True)
    billing_date = models.DateField(null=True, blank=True)
    billing_total = MoneyField()
    billing_debt = MoneyField()
    parsing_date = models.DateTimeField(null=True, blank=True)
    upload_date = models.DateTimeField(default=now)
    provider_number = models.CharField(max_length=50, blank=True)
    internal_tax = TaxField(default=Decimal('0.0417'))
    iva_tax = TaxField(default=Decimal('0.27'))
    other_tax = TaxField(default=Decimal('0.04'))
    notes = models.TextField(blank=True)
    created = models.DateField(auto_now_add=True)
    last_modified = models.DateField(auto_now=True)

    class ParseError(Exception):
        """The invoice could not be parsed."""

    class AdjustmentError(Exception):
        """The invoice could not be adjusted."""

    class NotifyError(Exception):
        """The users could not be notified."""

    @property
    def taxes(self):
        return self.internal_tax + self.iva_tax + self.other_tax

    @property
    def consumptions_total(self):
        result = self.consumption_set.aggregate(total=Sum('total'))['total']
        if not result:
            result = Decimal(0)
        return result

    @property
    def outcome_debt(self):
        return self.consumptions_total - self.billing_debt

    @property
    def outcome_total(self):
        return self.consumptions_total - self.billing_total

    @property
    def details(self, user=None):
        if user is None:
            user = FleetUser.objects.get(is_superuser=True)

        # group consumptions per leader
        data = OrderedDict()
        leaders = FleetUser.objects.filter(leader=user)
        for leader in leaders.order_by('first_name'):
            users = list(FleetUser.objects.filter(leader=leader)) + [leader]
            consumptions = self.consumption_set.filter(phone__user__in=users)
            if consumptions:
                total = consumptions.aggregate(total=Sum('total'))['total']
                data[leader] = {
                    'consumptions': consumptions.order_by('phone__number'),
                    'total': total,
                }

        return data

    def __str__(self):
        return 'Bill "%s" (date: %s)' % (self.fleet, self.billing_date)

    def _apply_partial_penalty(self, data, penalty, attr_name, attr_total):
        # sort ascending
        totals = pairwise(sorted(data.keys()))
        for total1, total2 in totals:
            # distribute penalty within the same category
            cons = data[total1]
            len_cons = Decimal(len(cons))

            diff = total2 - total1
            assert diff > 0  # because the key are ascendingly sorted

            to_apply = min(diff * len_cons, penalty)
            if to_apply == 0:
                logging.warning('Can not apply a 0 value as penalty.')
                msg = 'Penalty should be 0 by now, got %s instead'
                assert penalty == 0, msg % penalty
                break

            penalty -= to_apply

            to_apply = Decimal(to_apply / len_cons)
            assert to_apply > 0
            for c in cons:
                assert c is not None
                current = getattr(c, attr_name)
                setattr(c, attr_name, current + to_apply)
                assert penalty == 0 or getattr(c, attr_total) == total2

            if penalty > 0:
                # update data
                data.pop(total1)
                data[total2].extend(cons)
            else:
                break

        # penalty was fully applied, save all consumptions
        for consumptions in data.values():
            for c in consumptions:
                if c is not None:
                    c.save()

    def apply_penalty(self, consumptions, penalty):
        assert ((penalty.minutes > 0 and penalty.plan.included_min > 0) or
                (penalty.sms > 0 and penalty.plan.included_sms > 0))

        if consumptions.count() == 0:
            logging.warning('There is no consumption to apply the %s to.',
                            penalty)
            return

        # prioritize readable code over efficient code
        data_min = defaultdict(list)
        data_sms = defaultdict(list)
        for c in consumptions:
            if c.mins < c.plan.included_min:
                data_min[c.mins].append(c)  # group by used minutes

            if c.sms < c.plan.included_sms:
                data_sms[c.sms].append(c)  # group by used sms

        # need to add an extra key for the plan total, so when building
        # the pairwise generator, the last total has an entry of its own
        # Example: if we have consumptions to apply penalty to of
        # 30, 50, 80 and the plan target is 100, we need the following pairs:
        # [(30, 50), (50, 80), (80, 100)]
        data_min[Decimal(penalty.plan.included_min)].append(None)
        data_sms[Decimal(penalty.plan.included_sms)].append(None)

        self._apply_partial_penalty(
            data_min, penalty.minutes, 'penalty_min', 'total_min')
        self._apply_partial_penalty(
            data_sms, penalty.sms, 'penalty_sms', 'total_sms')

    @transaction.atomic()
    def parse_invoice(self, invoice_file_object):
        """Parse this bill's invoice.

        Return whether the parse was successful

        """
        if self.parsing_date is not None:
            raise Bill.ParseError('Invoice already parsed on %s.' %
                                  self.parsing_date)
        self.invoice_filename = getattr(
            invoice_file_object, 'name', 'No name in file descriptor')
        try:
            data = pdf2cell.parse_file(invoice_file_object)
        except pdf2cell.CellularDataParseError as e:
            raise Bill.ParseError(str(e))

        if not data:
            return

        bill_date = data.get('bill_date')
        if bill_date:
            self.billing_date = bill_date
        self.billing_debt = data.get('bill_debt', Decimal('0'))
        self.billing_total = data.get('bill_total', Decimal('0'))
        self.provider_number = data.get('bill_number', '')
        self.internal_tax = data.get('internal_tax', self.internal_tax)
        self.other_tax = data.get('other_tax', self.other_tax)

        for d in data.get('phone_data', []):
            try:
                phone = Phone.objects.get(number=d[PHONE_NUMBER])
            except Phone.DoesNotExist:
                raise Bill.ParseError('Phone %s does not exist.' %
                                      d[PHONE_NUMBER])

            plan = None
            if not plan:
                if not d[PLAN]:
                    # this phone is disappearing, so there should be a previous
                    # consumption with the plan info that serves for this item
                    logging.warning(
                        'Plan info for %r is not available from parsed data.',
                        phone)
                    plan = Consumption.objects.filter(phone=phone)
                    if plan.count() == 0:
                        raise Bill.ParseError('Plan info for %r is not '
                                              'available.' % phone)
                    plan = plan.latest().plan
                else:
                    try:
                        plan = Plan.objects.get(name=d[PLAN])
                    except Plan.DoesNotExist:
                        raise Bill.ParseError('Plan %s does not exist in DB.' %
                                              d[PLAN])

            kwargs = dict(
                reported_user=d[USER],
                reported_plan=d[PLAN],
                monthly_price=d[MONTHLY_PRICE],
                services=d[SERVICES],
                refunds=d[REFUNDS],
                included_min=d[INCLUDED_MIN],
                exceeded_min=d[EXCEEDED_MIN] + d[EXCEEDED_STABLISHING_MIN],
                exceeded_min_price=(
                    d[EXCEEDED_MIN_PRICE] + d[EXCEEDED_STABLISHING_MIN_PRICE]),
                ndl_min=d[NDL_MIN],
                ndl_min_price=d[NDL_PRICE],
                idl_min=d[IDL_MIN],
                idl_min_price=d[IDL_PRICE],
                sms=d[SMS],
                sms_price=d[SMS_PRICE],
                other_price=d[OTHER_PRICE],
                reported_total=d[TOTAL_PRICE],
            )
            Consumption.objects.create(phone=phone, bill=self, plan=plan,
                                       **kwargs)

        self.parsing_date = now()
        self.save()

    def calculate_penalties(self):
        """Calculate penalties per plan with clearing."""
        if self.parsing_date is None:
            raise Bill.AdjustmentError('Bill must be parsed before making '
                                       'adjustments.')

        plans = Plan.objects.filter(consumption__bill=self).distinct()
        for plan in plans:
            if Penalty.objects.filter(bill=self, plan=plan).count() > 0:
                logging.warning('Penalty for "%s" and "%s" already exists, '
                                'deleting.', self, plan)
                Penalty.objects.filter(bill=self, plan=plan).delete()

            consumptions = self.consumption_set.filter(plan=plan)
            if not consumptions:
                logging.info('There is no consumptions for "%s" and "%s".',
                             self, plan)
                continue

            # remove existing penalties if any, we may be recalculating
            consumptions.update(penalty_min=0, penalty_sms=0)

            diff_min = 0
            if plan.with_min_clearing:
                # decide if penalty for mins is needed
                target = plan.included_min * consumptions.count()
                real = consumptions.aggregate(mins=Sum('mins'))['mins']
                diff_min = max(target - real, 0)

            diff_sms = 0
            if plan.with_sms_clearing:
                # decide if penalty for sms is needed
                target = plan.included_sms * consumptions.count()
                real = consumptions.aggregate(sms=Sum('sms'))['sms']
                diff_sms = max(target - real, 0)

            # apply newly calculated penalties
            if diff_min > 0 or diff_sms > 0:
                penalty = Penalty.objects.create(
                    bill=self, plan=plan, minutes=diff_min, sms=diff_sms)
                self.apply_penalty(consumptions, penalty)

    def apply_delta(self, delta):
        self.consumption_set.update(extra=F('extra') + delta)
        for c in self.consumption_set.all():
            c.save()


class Plan(models.Model):
    """Phone line plan."""
    name = models.CharField(max_length=100)
    price = MoneyField()
    price_min = MoneyField()
    price_sms = MoneyField()
    included_min = models.PositiveIntegerField(default=0)
    included_sms = models.PositiveIntegerField(default=0)
    description = models.TextField(blank=True)
    with_min_clearing = models.BooleanField(default=True)
    with_sms_clearing = models.BooleanField(default=False)

    def __str__(self):
        return '%s - $%s' % (self.name, self.price)


class DataPack(models.Model):
    """Internet data pack."""
    kbs = models.PositiveIntegerField(blank=True, null=True)
    price = MoneyField()

    def __str__(self):
        kbs = '%s kbs' % self.kbs if self.kbs else '(unlimited)'
        return '%s - $%s + IMP' % (kbs, self.price)


class SMSPack(models.Model):
    """SMS pack."""
    units = SMSField()
    price = MoneyField()

    def __str__(self):
        return '%s sms - $%s + IMP' % (self.units, self.price)


class Phone(models.Model):
    """Phone line."""
    number = models.CharField(max_length=10)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    current_plan = models.ForeignKey(Plan, on_delete=models.CASCADE)
    data_pack = models.ForeignKey(
        DataPack, blank=True, null=True, on_delete=models.CASCADE)
    sms_pack = models.ForeignKey(
        SMSPack, blank=True, null=True, on_delete=models.CASCADE)
    notes = models.TextField(blank=True)
    active_since = models.DateTimeField(default=now)
    active_to = models.DateTimeField(null=True, blank=True)

    class Meta:
        get_latest_by = 'active_since'

    def __str__(self):
        result = str(self.number)
        if self.user.get_full_name():
            result += ' - %s' % self.user.get_full_name()
        return result

    @property
    def active(self):
        return self.active_to is None or self.active_to > now()


class Consumption(models.Model):
    """Phone line consumption for a bill."""
    phone = models.ForeignKey(Phone, on_delete=models.CASCADE)
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE)

    # Even though there is a FK to phone, the current plan for the phone may
    # not be the plan for this consumption (since phones may change its plan).
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE)

    # every field (literal) from the invoice
    reported_user = models.CharField('Usuario', max_length=500, blank=True)
    reported_plan = models.CharField('Plan oficial', max_length=5, blank=True)
    monthly_price = MoneyField('Precio del plan ($)')
    services = MoneyField('Cargos y servicios ($)')
    refunds = MoneyField('Reintegros ($)')
    included_min = MinuteField('Minutos consumidos incluidos en plan')
    exceeded_min = MinuteField('Minutos consumidos fuera del plan')
    exceeded_min_price = MoneyField('Minutos consumidos fuera del plan ($)')
    ndl_min = MinuteField('Discado nacional (minutos)')
    ndl_min_price = MoneyField('Discado nacional ($)')
    idl_min = MinuteField('Discado internacional (minutos)')
    idl_min_price = MoneyField('Discado internacional ($)')
    sms = SMSField('Mensajes consumidos')
    sms_price = MoneyField('Mensajes consumidos ($)')
    equipment_price = MoneyField('Equipos ($)')
    other_price = MoneyField('Varios ($)')
    reported_total = MoneyField('Total ($)')

    # calculated *and* stored in the DB
    penalty_min = MinuteField('Multa de minutos')
    penalty_sms = SMSField('Multa de mensajes')
    mins = MinuteField('Suma de minutos consumidos y excedentes, '
                       'antes de multas')
    total_before_taxes = MoneyField()
    taxes = TaxField(default=Decimal('0'))
    total_before_round = MoneyField()
    total = MoneyField()

    # added by hand if needed
    extra = MoneyField('Extra (por equipo/s, o IVA de equipo, etc.)')

    def __str__(self):
        return '%s - Bill from %s - Phone %s' % (self.bill.fleet.provider,
                                                 self.bill.billing_date,
                                                 self.phone)

    @property
    def total_min(self):
        """Suma de minutos consumidos y excedentes, y multas."""
        return self.mins + self.penalty_min

    @property
    def total_sms(self):
        """Suma de mensajes consumidos y multas."""
        return self.sms + self.penalty_sms

    def save(self, *args, **kwargs):
        self.mins = Decimal(self.included_min) + Decimal(self.exceeded_min)

        total = self.reported_total
        plan = self.plan
        if plan.with_min_clearing:
            total -= self.monthly_price
            # we now need the exceeded_min to be included since those seem to
            # be used against Claro lines
            # other_diff = abs(self.other_price - self.exceeded_min_price)
            total += (
                (Decimal(self.mins) + Decimal(self.penalty_min)) *
                Decimal(plan.price_min))

            if plan.with_sms_clearing:
                # calculate real amount of sms to be charged for
                total += (self.sms + self.penalty_sms) * plan.price_sms
                # XXX: potential issue: is there are not SMS penalties,
                # (i.e. all SMS were consumed), we need to substract the
                # exceeding SMS being charged in the sms_price column
                p = Penalty.objects.filter(bill=self.bill, plan=self.plan)
                if p.count() == 0 or p.get().sms == 0:
                    total -= self.sms_price

        self.total_before_taxes = total
        self.taxes = self.bill.taxes
        self.total_before_round = (
            self.total_before_taxes * (Decimal('1') + self.taxes) +
            self.extra)  # add any needed extra
        self.total = round(self.total_before_round)
        super(Consumption, self).save(*args, **kwargs)

    @property
    def used_min(self):
        """Return the really consumed minutes: included + exceeded."""
        return self.included_min + self.exceeded_min

    class Meta:
        get_latest_by = 'bill__billing_date'
        unique_together = ('phone', 'bill')


class Penalty(models.Model):
    """Penalty to be charged to phone lines in a plan for a bill."""
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE)
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE)
    minutes = MinuteField()
    sms = SMSField()

    class Meta:
        unique_together = ('bill', 'plan')

    def __str__(self):
        return 'Penalty of %s minutes for %s (%s)' % (self.minutes, self.bill,
                                                      self.plan)
