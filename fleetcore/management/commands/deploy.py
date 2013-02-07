# coding: utf-8

from __future__ import unicode_literals
from __future__ import print_function

import os
import subprocess

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


REPO_ROOT = os.path.join(settings.PROJECT_ROOT, '..')


class Command(BaseCommand):

    help = 'Update working copy, sync/migrate and collectstatic.'

    def handle(self, *args, **options):
        r = subprocess.call(['hg', 'update', '--cwd', REPO_ROOT])
        if r != 0:
            raise CommandError('Failed update.')

        call_command('syncdb', interactive=False)
        call_command('migrate', interactive=False)
        call_command('collectstatic', interactive=False)
        self.stdout.write('Successfully updated project.\n')
