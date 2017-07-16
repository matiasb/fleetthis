# coding: utf-8

import random
import string

from django.contrib.auth import get_user_model

from fleetcore.models import (
    Bill,
    Consumption,
    DataPack,
    Fleet,
    Penalty,
    Phone,
    Plan,
    SMSPack,
)


User = get_user_model()


class Factory(object):
    """A factory of models."""

    def make_random_string(self, length=10, only_digits=False):
        if only_digits:
            source = string.digits
        else:
            source = string.ascii_letters + string.digits
        return ''.join(random.choice(source) for i in range(length))

    def make_random_number(self, digits=3):
        return int(random.random() * (10 ** digits))

    def make_fleetuser(self, **kwargs):
        _kwargs = dict(username='username-%s' % self.make_random_number())
        _kwargs.update(kwargs)
        result = User.objects.create_user(**_kwargs)
        return result

    def make_admin_user(self, **kwargs):
        result = self.make_fleetuser(**kwargs)
        result.is_staff = True
        result.is_superuser = True
        result.save()
        return result

    def make_something(self, model_class, default, **kwargs):
        _kwargs = default.copy()
        _kwargs.update(kwargs)
        return model_class.objects.create(**_kwargs)

    def make_fleet(self, **kwargs):
        default = dict(
            user=self.make_fleetuser(),
            account_number=self.make_random_string(only_digits=True),
            provider=self.make_random_string())
        return self.make_something(Fleet, default, **kwargs)

    def make_bill(self, **kwargs):
        default = dict(fleet=self.make_fleet())
        return self.make_something(Bill, default, **kwargs)

    def make_plan(self, **kwargs):
        default = dict()
        return self.make_something(Plan, default, **kwargs)

    def make_datapack(self, **kwargs):
        default = dict()
        return self.make_something(DataPack, default, **kwargs)

    def make_smspack(self, **kwargs):
        default = dict(units=self.make_random_number())
        return self.make_something(SMSPack, default, **kwargs)

    def make_phone(self, user=None, **kwargs):
        if user is None:
            user = self.make_fleetuser()
        default = dict(
            number=self.make_random_string(only_digits=True),
            user=user, current_plan=self.make_plan())
        return self.make_something(Phone, default, **kwargs)

    def make_consumption(self, user=None, **kwargs):
        default = dict(bill=self.make_bill(),
                       phone=self.make_phone(user=user),
                       plan=self.make_plan())
        return self.make_something(Consumption, default, **kwargs)

    def make_penalty(self, **kwargs):
        default = dict(bill=self.make_bill(), plan=self.make_plan())
        return self.make_something(Penalty, default, **kwargs)
