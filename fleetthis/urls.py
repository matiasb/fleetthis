# coding: utf-8

from __future__ import print_function, unicode_literals

from django.conf.urls import patterns, include, url
from django.contrib import admin

import fleetcore.views


admin.autodiscover()


urlpatterns = [
    url(r'^$', fleetcore.views.home, name='home'),
    url(r'^fleetcore/', include('fleetcore.urls')),
    url(r'^admin/', include(admin.site.urls)),
]
