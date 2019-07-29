django-storedqueries
====================

:author: Keryn Knight
:version: 0.1.3

A small package for `Django`_ to ease the creation of database temporary tables.

It doesn't need to be in your ``INSTALLED_APPS``

Usage
-----

Define a mostly normal Django model, like so::

    from django.db import models

    class MyCoolModel(models.Model):
        value = models.PositiveIntegerField(primary_key=True)

        class Meta:
            abstract = True
            managed = False

Pay special attention to the ``Meta`` attributes. It'll complain otherwise.

Provide a definition for the temporary table somewhere::

    from storedqueries import TemporaryTable

    class MyTemporaryTable(TemporaryTable):
        model = MyCoolModel
        queryset = Somedata.objects.order_by('?').annotate(value=models.F('key_name')).values_list('value').iterator()

Make use of the temporary table::

    from django.http import JsonResponse

    def myview(request, *args, **kwargs):
        with MyTemporaryTable() as TemporaryModel:
            keys = TemporaryModel.objects.all()
            data = tuple(Somedata.objects.filter(key_name__in=keys))
            return JsonResponse({'values': data})

Using the ``with my_cls() as thing:`` syntax will create a uniquely named
temporary table using the ``queryset`` connection and data to fill it,
when the ``with`` scope closes, the temporary table is dropped. The
``TemporaryModel`` variable will be a **subclass** of ``MyCoolModel`` bound to
the unique name for the temporary table.

If you have a query which cannot be defined at module scope, you can do::

    class MyTemporaryTable(TemporaryTable):
        model = MyCoolModel
        def source_queryset(self):
            return Somedata.objects.filter(created__lte=timezone.now()).annotate(value=models.F('key_name')).values_list('value').iterator()

If you **still** cannot get the query correct, because it has a dependency
on something like ``request.user`` etc, you can do::

    def myview(request, *args, **kwargs):
        qs = Somedata.objects.filter(user=request.user.pk)
        with MyTemporaryTable(queryset=qs) as TemporaryModel:
            raise NotImplementedError("Dynamic queryset binding")

or probably even::

    def myview(request, *args, **kwargs):
        qs = Somedata.objects.filter(user=request.user.pk)
        with TemporaryTable(model=MyCoolModel, queryset=qs) as TemporaryModel:
            raise NotImplementedError("Dynamic model AND queryset binding")

The license
-----------

It's `FreeBSD`_. There's should be a ``LICENSE`` file in the root of the repository, and in any archives.

.. _FreeBSD: http://en.wikipedia.org/wiki/BSD_licenses#2-clause_license_.28.22Simplified_BSD_License.22_or_.22FreeBSD_License.22.29
.. _Django: https://www.djangoproject.com/
