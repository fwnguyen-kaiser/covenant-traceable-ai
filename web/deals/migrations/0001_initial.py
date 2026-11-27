"""Create the non-table-owning report.release Django permission cleanly."""
from django.db import migrations


def create_release_permission(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(app_label="deals", model="reportrelease")
    Permission.objects.get_or_create(
        content_type=ct,
        codename="release",
        defaults={"name": "Can release reviewed valuation reports"},
    )


def remove_release_permission(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="deals", model="reportrelease").delete()
    Permission.objects.filter(codename="release", content_type__app_label="deals").delete()


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("auth", "__latest__"),
        ("contenttypes", "__latest__"),
    ]

    operations = [
        migrations.RunPython(create_release_permission, remove_release_permission),
    ]
