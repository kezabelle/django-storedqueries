# -*- coding: utf-8 -*-
from __future__ import unicode_literals, absolute_import
from collections import namedtuple

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Model, QuerySet
from django.db.models.sql.compiler import SQLCompiler
from enum import Enum

try:
    from typing import Optional, Text, Type
except ImportError:
    pass


__all__ = [
    "TemporaryTable",
    "TemporaryTableEditor",
    "temporary_table",
    "DatabaseSpecifics",
    "PostgresSpecifics",
    "SqliteSpecifics",
    "MySqlSpecifics",
    "StatusError",
    "TemporaryTableEditorStatus",
]


class TemporaryTable(object):
    """
    A declarative wrapper object for setting up a queryset to be turned into
    a temporary table accessible as an ORM model.

    Example:

    class MyCoolTable(TemporaryTable):
        model = MyCoolModel
        queryset = MyUncoolData.objects.filter(pk__in=(1,3,3,7)).values_list('pk', flat=True).iterator()

    class MyCoolTable2(TemporaryTable):
        model = MyCoolModel
        def source_queryset(self):
            return MyUncoolData.objects.filter(created__lt=timezone.now()).values_list('pk', flat=True)
    """

    model = None  # type: Optional[Model]
    queryset = None  # type: Optional[QuerySet]
    name = None  # type: Optional[Text]

    def __init__(self, model=None, queryset=None, name=None):
        self.model = model
        self.queryset = queryset
        self.name = name

    def target_model(self):
        return self.model

    def source_queryset(self):
        return self.queryset

    def target_name(self):
        return self.name


DatabaseSpecifics = namedtuple("DatabaseSpecifics", ("sql_create", "sql_drop"))
PostgresSpecifics = DatabaseSpecifics(
    sql_create="CREATE TEMPORARY TABLE %(table)s AS (%(definition)s)",
    sql_drop="DROP TABLE IF EXISTS %(table)s",
)
SqliteSpecifics = PostgresSpecifics
MySqlSpecifics = DatabaseSpecifics(
    sql_create="CREATE TEMPORARY TABLE %(table)s AS (%(definition)s)",
    # Yay MySQL actually has the best/least-terrifying syntax!
    sql_drop="DROP TEMPORARY TABLE IF EXISTS %(table)s",
)


class TemporaryTableEditorStatus(Enum):
    UNKNOWN = 1
    OPENED = 2
    CLOSED = 3


class StatusError(ValueError):
    pass


class TemporaryTableEditor(object):
    """
    Handler for creating and destroying a temporary database table and providing
    a temporary Model class which is backed by said table.
    The exposed implementation is fairly minimal, in that the only methods
    you might want to call are open() and close()

    Tidiest usage (creates and drops the table for you via a context manager):

    with temporary_table(MyCoolTable()) as MyTemporaryCoolModel:
        data = tuple(MyTemporaryCoolModel.objects.all())

    Alternative (no context manager):
        temporary_table = TemporaryTableEditor(MyCoolTable())
        MyTemporaryCoolModel = temporary_table.open()
        data = tuple(MyTemporaryCoolModel.objects.all())
        temporary_table.close()

    Or, with the stdlib closing context manager:
        temporary_table = TemporaryTableEditor(MyCoolTable())
        MyTemporaryCoolModel = temporary_table.open()
        with closing(temporary_table):
            data = tuple(MyTemporaryCoolModel.objects.all())

    To get it to be dropped at the end of the request, you need to add it to
    the secret _closable_objects API, like so:

        MyTemporaryCoolModel = temporary_table.open()
        data = tuple(MyTemporaryCoolModel.objects.all())
        request._closable_objects.append(temporary_table)

    To get it dropped at the end of a transaction, you can do:
        with transaction.atomic():
            MyTemporaryCoolModel = temporary_table.open()
            data = tuple(MyTemporaryCoolModel.objects.all())
            transaction.on_commit(temporary_table.close)
            MyCoolTable.objects.filter(pk=1).update(name='supercool')
    Though be aware that if the transaction is rolled back, it won't be dropped
    and will be implicitly dropped at the end of the "session" which could be
    much later, and probably could bleed across requests if using MAX_CONN_AGE
    """

    __slots__ = ("temporary_table", "status")

    def __init__(self, temporary_table):
        # type: (TemporaryTable) -> None
        """
        Accepts a TemporaryTable or anything which implements:
        target_model()
        source_queryset()
        target_name()

        For example, this is a valid input, if you so wish:

        class X(namedtuple('X', 'm m2')):
            def target_model(self): return self.m
            def target_name(self): return uuid4()
            def source_queryset(self): return self.m2.__class__.objects.filter(pk=1)
        x = X(m=MyCoolData, m2=MyUnCoolDataSource)

        They're methods so I can expand them to accept args/kwargs if necessary
        once I establish more use cases. It's possible they'll also turn into
        properties if no more dynamic requirements turn up.
        """
        self.temporary_table = temporary_table
        model_class = self.temporary_table.target_model()
        if model_class is None:
            raise ImproperlyConfigured(
                "target_model() for {mod!s}.{cls!s} returned None; did you forget to set model = ... or override the method?".format(
                    mod=model_class.__module__, cls=model_class.__name__
                )
            )
        if not model_class._meta.abstract:
            raise ImproperlyConfigured(
                "class Meta for {mod!s}.{cls!s} must define abstract=True".format(
                    mod=model_class.__module__, cls=model_class.__name__
                )
            )
        if model_class._meta.managed:
            raise ImproperlyConfigured(
                "class Meta for {mod!s}.{cls!s} must define managed=False".format(
                    mod=model_class.__module__, cls=model_class.__name__
                )
            )
        if not self.temporary_table.target_name():
            raise ImproperlyConfigured(
                "target_name() for {mod!s}.{cls!s} was falsy, did you forget to set name = '...' or override the method?".format(
                    mod=model_class.__module__, cls=model_class.__name__
                )
            )
        if self.temporary_table.source_queryset() is None:
            raise ImproperlyConfigured(
                "source_queryset() for {mod!s}.{cls!s} returned None, did you forget to set queryset = ... or override the method?".format(
                    mod=model_class.__module__, cls=model_class.__name__
                )
            )
        self.status = TemporaryTableEditorStatus.UNKNOWN

    def operations(self, vendor):
        # type: (str) -> DatabaseSpecifics
        """
        Given a vendor string, which can be be obtained from the current
        DatabaseWrapper instance, return the SQL templates necessary for
        setting up and destroying a temporary table.
        """
        vendors = {
            "mysql": MySqlSpecifics,
            "postgresql": PostgresSpecifics,
            "sqlite": SqliteSpecifics,
        }
        if vendor not in vendors:
            raise NotImplementedError(
                "I don't know about the vendor string: {!r}".format(vendor)
            )
        return vendors[vendor]

    def temporary_model(self):
        # type: () -> Type[Model]
        """
        Returns a subclass of the model defined on the TemporaryTable (or whatever)
        whose Meta has been swizzled to be correct for the SQL table we'll be
        making.
        """
        klass = self.temporary_table.target_model()
        table_name = self.temporary_table.target_name()
        fake_app = "__temporary_tables_{!s}__".format(table_name)
        fake_model_name = str(klass.__name__ + "TempTable")
        try:
            model_cls = apps.get_registered_model(fake_app, fake_model_name)
            assert model_cls._meta.abstract is False, "Not abstract"
            assert model_cls._meta.db_table == table_name, "Differing db_table"
            assert model_cls._meta.app_label == fake_app, "Differing app_label"
            assert model_cls.__name__ == fake_model_name, "Differing cls name"
        except (LookupError, AssertionError) as e:

            class Meta:
                # Has to be False to get a default manager etc...
                # But then it ends up in apps.register_model() via Model.__new__
                abstract = False
                db_table = table_name
                app_label = fake_app

            model_cls = type(
                fake_model_name, (klass,), {"Meta": Meta, "__module__": fake_app}
            )
        return model_cls

    def open(self):
        # type: () -> Type[Model]
        """
        Creates a named temporary table using the syntax appropriate for the
        queryset's vendor (postgresql/mysql ...) and add any indexes defined,
        using the migrations package.

        Returns a Model for accessing the table, which will only work for the
        lifetime of this TemporaryTableEditor's usage (ie: until .close() is called)
        """
        if self.status == TemporaryTableEditorStatus.OPENED:
            raise StatusError("Already in an opened state")
        model = self.temporary_model()
        table_name = model._meta.db_table  # type: str
        qs = self.temporary_table.source_queryset()
        compiler = qs.query.get_compiler(qs.db)  # type: SQLCompiler
        connection = compiler.connection  # type: BaseDatabaseWrapper
        alias = connection.vendor  # type: str
        sql_create, sql_drop = self.operations(alias)
        query, query_params = compiler.as_sql()
        table = sql_create % dict(
            table=connection.ops.quote_name(table_name), definition=query
        )

        schema_editor = connection.schema_editor()  # type: BaseDatabaseSchemaEditor
        indexes = schema_editor._model_indexes_sql(model)
        with connection.cursor() as c:
            c.execute(table, query_params)
            if indexes:
                for index in indexes:
                    c.execute(index)
        self.status = TemporaryTableEditorStatus.OPENED
        return self.temporary_model()

    def close(self):
        # type: () -> int
        """
        Destroy/drop the temporary table, which should automatically include
        dropping any declared indexes.
        """
        if not self.status == TemporaryTableEditorStatus.OPENED:
            raise StatusError("Not in an opened state")
        table_name = self.temporary_table.target_name()
        qs = self.temporary_table.source_queryset()
        compiler = qs.query.get_compiler(qs.db)  # type: SQLCompiler
        connection = compiler.connection  # type: BaseDatabaseWrapper
        alias = connection.vendor  # type: str
        sql_create, sql_drop = self.operations(alias)
        table = sql_drop % dict(table=compiler.connection.ops.quote_name(table_name))
        with connection.cursor() as c:
            c.execute(table)
        self.status = TemporaryTableEditorStatus.CLOSED
        return self.status

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# Have a nicer name exported for use via a context manager
temporary_table = TemporaryTableEditor
