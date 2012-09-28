# coding: utf-8

from __future__ import unicode_literals
from __future__ import print_function

import logging
import os

from unittest import TestCase

from django.contrib.auth.models import User
from django.core.files import File
from mock import patch

from fleetcore.models import Bill, Consumption, Fleet
from fleetcore.admin import (
    BillAdmin,
)


class ParceInvoiceTestCase(TestCase):
    """The test suite for the parse_invoice method."""

    def setUp(self):
        super(ParceInvoiceTestCase, self).setUp()
        patcher = patch('fleetcore.models.pdf2cell.parse_file')
        self.mock_pdf_parser = patcher.start()
        self.addCleanup(patcher.stop)

        self.owner = User.objects.create(username='owner',
                                         email='owner@example.com')

        self.fleet = Fleet.objects.create(
            owner=self.owner, account_number=123456,
            email='foo@example.com', provider='Fake')

    def test_empty_bill_is_parsed_when_created(self):
        assert Consumption.objects.count() == 0

        self.mock_pdf_parser.return_value = {}
        bill = Bill.objects.create(
            fleet=self.fleet,
            invoice=File(open(__file__), "test_invoice.pdf"))

        self.mock_pdf_parser.assert_called_once_with(bill.invoice.path)
        self.assertEqual(Consumption.objects.count(), 0)
