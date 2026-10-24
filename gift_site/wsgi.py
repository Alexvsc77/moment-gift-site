"""WSGI entry point for the gift site."""

import os

from django.core.wsgi import get_wsgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "gift_site.settings")
application = get_wsgi_application()
