# Generated by Django 2.0.2 on 2018-02-19 20:40

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleetcore', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='fleetuser',
            name='last_name',
            field=models.CharField(blank=True, max_length=150, verbose_name='last name'),
        ),
    ]