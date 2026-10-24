#!/usr/bin/env python3
"""Command-line entry point for the gift site."""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "gift_site.settings")

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django is not installed. Install the packages in requirements.txt first."
        ) from exc

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
