# Generated by Django 4.2.18 on 2025-02-03 13:29

from django.db import migrations, models


class Migration(migrations.Migration):

    replaces = [
        ('issues', '0004_rename_event_count_issue_digested_event_count'),
        ('issues', '0005_rename_ingest_order_issue_digest_order_and_more'),
        ('issues', '0006_issue_next_unmute_check'),
        ('issues', '0007_alter_turningpoint_options'),
    ]

    dependencies = [
        ("projects", "0002_b_squashed_initial"),
        ("issues", "0003_alter_turningpoint_triggering_event"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="turningpoint",
            options={"ordering": ["-timestamp", "-id"]},
        ),
        migrations.RenameField(
            model_name="issue",
            old_name="ingest_order",
            new_name="digest_order",
        ),
        migrations.RenameField(
            model_name="issue",
            old_name="event_count",
            new_name="digested_event_count",
        ),
        migrations.AlterUniqueTogether(
            name="issue",
            unique_together={("project", "digest_order")},
        ),
        migrations.AddField(
            model_name="issue",
            name="next_unmute_check",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
