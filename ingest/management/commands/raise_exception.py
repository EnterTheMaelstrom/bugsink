from sentry_sdk.hub import GLOBAL_HUB

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Quick and dirty command to just raise an exception and see it show up in Bugsink"

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-release",
            action="store_true",
            help="Set the value of the sent release to None",
        )

    def handle(self, *args, **options):
        from sentry_sdk import set_tag
        set_tag("foo", "bar")
        set_tag("baz", 1)

        if options["no_release"]:
            # The sentry client "tries hard" to get the release from the environment (including git); I have found the
            # following to be a workable way to set the release to None. (But I have checked that, in the absence of
            # .git, the release is set to None by default by the Python sentry sdk.)
            GLOBAL_HUB.client.options['release'] = None

        try:
            self.raise_exception("Exception raised on purpose - 2")
        except Exception as e:
            # self.raise_exception("An 'accident' happened while handling the exception")
            self.raise_exception_from("We intentionally translated this into the exception", e)

    def raise_exception(self, msg):
        raise Exception(msg)

    def raise_exception_from(self, msg, e):
        s = "foo"
        l = ["bar", 1, None, ["bar", 1, None]]
        d = {
            "foo": "bar",
            "baz": 1,
            "snu": None,
            "recurse": {
                "foo": "bar",
                "baz": 1,
                "snu": None,
            },
        }
        raise Exception(msg) from e
