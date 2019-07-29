# -*- coding: utf-8 -*-
from __future__ import unicode_literals, absolute_import
from collections import namedtuple
from functools import partial

try:
    from django.apps import apps

    get_models = partial(
        apps.get_models,
        include_auto_created=True,
        include_deferred=True,
        include_swapped=True,
    )
except ImportError:
    from django.db.models import get_models as _get_models

    get_models = partial(
        _get_models,
        include_auto_created=True,
        include_deferred=True,
        only_installed=False,
    )
from django.core.exceptions import ImproperlyConfigured
from django.core.management.color import no_style

try:
    from typing import Optional, Text, Type, TYPE_CHECKING
except ImportError:
    TYPE_CHECKING = False
try:
    from django.db.backends.base.base import BaseDatabaseWrapper
except ImportError:
    from django.db.backends import BaseDatabaseWrapper
try:
    PRE_MIGRATIONS = False
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
except ImportError:
    PRE_MIGRATIONS = True
    from django.db.backends.creation import BaseDatabaseCreation

from django.db.models import Model
from django.db.models.query import QuerySet

from django.db.models.sql.compiler import SQLCompiler

VERSION = "0.1.3"
__version_info__ = VERSION
__version__ = VERSION
version = VERSION
__all__ = [
    "TemporaryTable",
    "TemporaryTableEditor",
    "temporary_table",
    "StatusError",
    "version",
    "get_version",
]


def get_version():
    return tuple(int(x) for x in version.split("."))


class TemporaryTable(object):
    """
    A declarative wrapper object for setting up a queryset to be turned into
    a temporary table accessible as an ORM model.

    Example:
    ```
    class MyCoolTable(TemporaryTable):
        model = MyCoolModel
        queryset = MyUncoolData.objects.filter(pk__in=(1,3,3,7)).values_list('pk', flat=True).iterator()

    class MyCoolTable2(TemporaryTable):
        model = MyCoolModel
        def source_queryset(self):
            return MyUncoolData.objects.filter(created__lt=timezone.now()).values_list('pk', flat=True)
    ```
    It may subsequently be accessed like so:
    ```
    with MyCoolTable2(name="test") as MyTemporaryCoolModel:
        data = tuple(MyTemporaryCoolModel.objects.all())
    ```
    It can also be passed to a `TemporaryTableEditor`, which does the actual work
    of turning this object's attributes into a temporary table. See the example
    usage in the `TemporaryTableEditor` docstring.
    """

    model = None  # type: Optional[Model]
    queryset = None  # type: Optional[QuerySet]
    name = None  # type: Optional[Text]
    _editor = None  # type: Optional[TemporaryTableEditor]

    def __init__(self, model=None, queryset=None, name=None):
        """
        When creating an instance of a temporary table, it is possible to swap
        out any class attributes defined to dynamically change the scope this
        will work with. Useful mostly for querysets which might depend on the
        `request.user`, or temporal data (dates/times) etc.
        """
        if model is not None:
            self.model = model
        if queryset is not None:
            self.queryset = queryset
        if name is not None:
            self.name = name
        self._editor = None

    def target_model(self):
        """
        Provide a normal Django `models.Model` subclass which defines all
        (or a subset of) the columns/attributes which should available after
        being mapped from the source_queryset.
        """
        return self.model

    def source_queryset(self):
        """
        Should return a Django `QuerySet` which will be used for the data in
        the temporary table.
        eg: `CREATE TEMPORARY TABLE "example" AS (source_queryset_sql_goes_here);`
        """
        return self.queryset

    def target_name(self):
        """
        Generate a unique name for the temporary table for this "instance" of it.
        Should be stable for the lifetime of the table, because this method
        may be called multiple times.
        """
        template = "tmp_{!s}_{!s}"
        unique = id(self)
        if self.name is not None:
            return template.format(self.name, unique)
        model = self.target_model()
        if model is not None:
            return template.format(model._meta.db_table, unique)
        return template.format("unknown_model_name", unique)

    def __enter__(self):
        self._editor = TemporaryTableEditor(self)
        temporary_model = self._editor.open()
        return temporary_model

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._editor.close()
        # Unbind the reference which was set up, to put this into an invalid
        # state until entering again ...
        # We may want to destroy anything in self.__dict__ for model/queryset/name
        # if it's not also in self.__class__.__dict__ but I'm not sure yet.
        self._editor = None


DatabaseSpecifics = namedtuple("DatabaseSpecifics", ("sql_create", "sql_drop"))
PostgresSpecifics = DatabaseSpecifics(
    # Postgres only allows for column *names*, not definitions, in the statement
    sql_create="CREATE TEMPORARY TABLE %(table)s AS (%(definition)s)",
    # Boo hiss! Postgres doesn't like the word TEMPORARY to appear here
    sql_drop="DROP TABLE IF EXISTS %(table)s",
)
SqliteSpecifics = DatabaseSpecifics(
    # SQLite doesn't want () around the select-stmt.
    sql_create="CREATE TEMPORARY TABLE %(table)s AS %(definition)s",
    # Boo hiss! SQLite doesn't like the word TEMPORARY to appear here
    sql_drop="DROP TABLE IF EXISTS %(table)s",
)
MySqlSpecifics = DatabaseSpecifics(
    # MySQL allows for the table definition to be provided as part of the statement
    # Note also that setting ENGINE=MEMORY appears to be faster, but cannot
    # have foreign keys or blob/text columns. Possibly that could be inferred
    # from the target_model definition ...
    sql_create="CREATE TEMPORARY TABLE %(table)s (%(table_def)s) ENGINE='%(mysql_engine)s' AS (%(definition)s)",
    # Yay MySQL actually has the best/least-terrifying syntax!
    sql_drop="DROP TEMPORARY TABLE IF EXISTS %(table)s",
)


class TemporaryTableEditorStatus(object):
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
    you might want to call are `open()` and `close()`

    Tidiest usage (creates and drops the table for you via a context manager):
    ```
    with temporary_table(MyCoolTable()) as MyTemporaryCoolModel:
        data = tuple(MyTemporaryCoolModel.objects.all())
    ```
    Alternative (no context manager):
    ```
    temporary_table = TemporaryTableEditor(MyCoolTable())
    MyTemporaryCoolModel = temporary_table.open()
    data = tuple(MyTemporaryCoolModel.objects.all())
    temporary_table.close()
    ```
    Or, with the stdlib closing context manager:
    ```
    temporary_table = TemporaryTableEditor(MyCoolTable())
    MyTemporaryCoolModel = temporary_table.open()
    with closing(temporary_table):
        data = tuple(MyTemporaryCoolModel.objects.all())
    ```
    To get it to be dropped at the end of the request, you need to add it to
    the secret `_closable_objects` API, like so:
    ```
    MyTemporaryCoolModel = temporary_table.open()
    data = tuple(MyTemporaryCoolModel.objects.all())
    request._closable_objects.append(temporary_table)
    ```
    To get it dropped at the end of a transaction, you can do:
    ```
    with transaction.atomic():
        MyTemporaryCoolModel = temporary_table.open()
        data = tuple(MyTemporaryCoolModel.objects.all())
        transaction.on_commit(temporary_table.close)
        MyCoolTable.objects.filter(pk=1).update(name='supercool')
    ```
    Though be aware that if the transaction is rolled back, it won't be dropped
    and will be implicitly dropped at the end of the "session" which could be
    much later, and probably could bleed across requests if using MAX_CONN_AGE
    """

    __slots__ = ("temporary_table", "status")

    def __init__(self, tmp_tbl):
        # type: (TemporaryTable) -> None
        """
        Accepts a TemporaryTable or anything which implements:
        `target_model()`
        `source_queryset()`
        `target_name()`

        For example, this is a valid input, if you so wish:
        ```
        class X(namedtuple('X', 'm m2')):
            def target_model(self): return self.m
            def target_name(self): return uuid4()
            def source_queryset(self): return self.m2.__class__.objects.filter(pk=1)
        x = X(m=MyCoolData, m2=MyUnCoolDataSource)
        with x as MyTemporaryCoolModel:
            data = tuple(MyTemporaryCoolModel.objects.all())
        ```
        They're methods so I can expand them to accept args/kwargs if necessary
        once I establish more use cases. It's possible they'll also turn into
        properties if no more dynamic requirements turn up.
        """
        self.temporary_table = tmp_tbl
        model_class = self.temporary_table.target_model()
        if model_class is None:
            raise ImproperlyConfigured(
                "target_model() for {obj!r} returned None; did you forget to set model = ... or override the method?".format(
                    obj=tmp_tbl
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
        hidden_related = {
            x.rel.is_hidden()
            for x in model_class._meta.fields
            if getattr(x, "rel", None) is not None
        }
        if not all(hidden_related):
            raise ImproperlyConfigured(
                "{mod!s}.{cls!s} must have related_name='+' for any related fields (ForeignKeys, etc)".format(
                    mod=model_class.__module__, cls=model_class.__name__
                )
            )
        if not self.temporary_table.target_name():
            raise ImproperlyConfigured(
                "target_name() for {obj!r} was falsy, did you forget to set name = '...' or override the method?".format(
                    obj=tmp_tbl
                )
            )
        if self.temporary_table.source_queryset() is None:
            raise ImproperlyConfigured(
                "source_queryset() for {obj!r} returned None, did you forget to set queryset = ... or override the method?".format(
                    obj=tmp_tbl
                )
            )
        self.status = TemporaryTableEditorStatus.UNKNOWN

    def operations(self, vendor):
        # type: (str) -> DatabaseSpecifics
        """
        Given a vendor string, which can be be obtained from the current
        `DatabaseWrapper` instance, return the SQL templates necessary for
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
        Returns a subclass of the model defined on the `TemporaryTable` (or whatever)
        whose Meta has been swizzled to be correct for the SQL table we'll be
        making.
        """
        klass = self.temporary_table.target_model()
        table_name = self.temporary_table.target_name()
        fake_app = "__temporary_tables_{!s}__".format(table_name)
        fake_model_name = str(klass.__name__ + "TempTable")

        class Meta:
            # Set it to abstract initially, which allows it to skip preparing
            # and precludes this temporary model going into the registered models
            # for this imaginary app label.
            abstract = True
            managed = True
            db_table = table_name
            app_label = fake_app

        # noinspection PyTypeChecker
        model_cls = type(
            fake_model_name, (klass,), {"Meta": Meta, "__module__": fake_app}
        )  # type: Type[Model]
        # `Model.__new__` has been called, and now we have our type, but it's
        # incomplete and won't work if it remains abstract, and it needs to
        # be prepared postfix manually to avoid going into the registered models
        # for this imaginary app.
        model_cls._meta.abstract = False
        model_cls._prepare()
        return model_cls

    def open(self):
        # type: () -> Type[Model]
        """
        Creates a named temporary table using the syntax appropriate for the
        queryset's vendor (postgresql/mysql ...) and add any indexes defined,
        using the migrations package.

        Returns a Model for accessing the table, which will only work for the
        lifetime of this `TemporaryTableEditor` instance's usage (ie: until
        `.close()` is called)
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

        # Only MySQL has an actually sane syntax for dropping tables which can
        # potentially shadow actual proper tables to prevent data loss. So
        # I'm just going to error if it shadows a known name otherwise.
        if sql_drop[0:10] == "DROP TABLE":
            model_tables = {x._meta.db_table for x in get_models()}
            if table_name in model_tables:
                raise ValueError(
                    "{!r} returned '{!s}' from target_name(); this table name is already used by an actual Model, and shadowing other tables is currently not allowed for '{!s}' because the syntax for dropping temporary tables is the same as normal tables".format(
                        type(self.temporary_table), table_name, alias
                    )
                )

        # If using MySQL, it can specify a preferred "ENGINE" to use. Ideally
        # we'd want to use MEMORY, but that only works for things which
        # don't include like like blob/text
        # https://dev.mysql.com/doc/refman/8.0/en/memory-storage-engine.html#memory-storage-engine-characteristics-of-memory-tables
        mysql_engine = "MEMORY"
        if "%(mysql_engine)s" in sql_create:
            field_types = (x.db_type(connection) for x in model._meta.fields)
            unsupported_types = {
                "tinyblob",
                "blob",
                "mediumblob",
                "longblob",
                "tinytext",
                "text",
                "mediumtext",
                "longtext",
            }
            bad_types = (x in unsupported_types for x in field_types)
            if any(bad_types):
                mysql_engine = "DEFAULT"

        # Only MySQL has the ability to set the column definitions as part
        # of a temporary table creation.
        # https://www.sqlite.org/lang_createtable.html
        # https://www.postgresql.org/docs/current/sql-createtableas.html
        # https://dev.mysql.com/doc/refman/8.0/en/create-table.html
        create_def = None  # type: Optional[str]
        if "%(table_def)s" in sql_create:
            if not PRE_MIGRATIONS:
                schema_editor = (
                    connection.schema_editor()
                )  # type: BaseDatabaseSchemaEditor
                column_sqls = []
                for field in model._meta.fields:
                    definition, params = schema_editor.column_sql(model, field)
                    column_sqls.append(
                        "%s %s" % (schema_editor.quote_name(field.column), definition)
                    )
                    # TODO: query params extension??
                    if params:
                        raise NotImplementedError(
                            "default values aren't implemented yet ..."
                        )
                create_def = "\n".join(column_sqls)
            else:
                creator = connection.creation  # type: BaseDatabaseCreation
                data, refs = creator.sql_create_model(model, no_style())
                # Get the field definitions, without "CREATE TABLE (...)"
                create_def = data[0].partition("(")[2].rpartition(")")[0]

        # Creating indexes may be expensive, but at least it's supported.
        if not PRE_MIGRATIONS:
            schema_editor = connection.schema_editor()
            indexes = schema_editor._model_indexes_sql(model)
        else:
            creator = connection.creation
            indexes = creator.sql_indexes_for_model(model, no_style())

        table = sql_create % dict(
            table=connection.ops.quote_name(table_name),
            definition=query,
            table_def=create_def,
            mysql_engine=mysql_engine,
        )
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
