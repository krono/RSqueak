from rsqueakvm.model.pointers import W_PointersObject
from rsqueakvm.plugins.database import dbm
from rsqueakvm.error import PrimitiveFailedError

from rpython.rlib import jit


class DBType(object):
    pass
NIL = DBType()
TEXT = DBType()
INTEGER = DBType()
REAL = DBType()
BLOB = DBType()

ALTER_SQL = "ALTER TABLE %s ADD COLUMN inst_var_%s %s;"
CREATE_SQL = "CREATE TABLE IF NOT EXISTS %s (id INTEGER);"
INSERT_SQL = "INSERT INTO %s (id) VALUES (?);"
SELECT_SQL = "SELECT inst_var_%s FROM %s WHERE id=?;"
UPDATE_SQL = "UPDATE %s SET inst_var_%s=? WHERE id=?"


@jit.elidable
def insert_sql(class_name):
    return INSERT_SQL % class_name


@jit.elidable
def select_sql(class_name, n0):
    return SELECT_SQL % (n0, class_name)


@jit.elidable
def alter_sql(class_name, n0, dbtype):
    if dbtype is NIL:
        strtype = ""
    elif dbtype is TEXT:
        strtype = "text"
    elif dbtype is INTEGER:
        strtype = "integer"
    elif dbtype is REAL:
        strtype = "real"
    elif dbtype is BLOB:
        strtype = "blob"
    else:
        assert False
    return ALTER_SQL % (class_name, n0, strtype)


@jit.elidable
def update_sql(class_name, n0):
    return UPDATE_SQL % (class_name, n0)


@jit.elidable
def create_sql(class_name):
    return CREATE_SQL % class_name


class W_DBObject_State:
    _immutable_fields_ = ["db_connection?", "column_types_for_table",
                          "db_objects", "class_names"]

    def __init__(self):
        self.id_counter = 0
        self.column_types_for_table = {}
        # Maps from DBObject id to DBObject and only includes DBObjects which
        # are referenced from an attribute of a DBObject.
        self.db_objects = {}
        self.class_names = {}

    def get_column_type(self, class_name, n0):
        dbtype = self.get_column_types(class_name)[n0]
        if dbtype != NIL:
            return jit.promote(dbtype)
        else:
            return NIL

    @jit.elidable
    def get_column_types(self, class_name):
        return self.column_types_for_table[class_name]

    def set_column_type(self, class_name, position, value):
        self.get_column_types(class_name)[position] = value

    # This is only ever called once per classname. We always promote the
    # classname to a constant, so any time the classname changes, we have to
    # break out of the trace and compile a new bridge, anyway. When that
    # happens, this was already run once, so we don't need to do it again.
    @jit.not_in_trace
    def init_column_types_if_neccessary(self, class_name, size):
        if class_name not in self.column_types_for_table:
            W_DBObject.state.column_types_for_table[class_name] = [NIL] * size

    # Same reason as above
    @jit.not_in_trace
    def create_table_if_neccessary(self, space, class_name, connection):
        if class_name not in W_DBObject.state.class_names:
            connection.execute(space, create_sql(class_name))
            W_DBObject.state.class_names[class_name] = True


class W_DBObject(W_PointersObject):
    _attrs_ = ["id", "w_id", "ivar_cache"]
    _immutable_fields_ = ["id", "w_id"]
    state = W_DBObject_State()
    repr_classname = "W_DBObject"

    @staticmethod
    def next_id():
        theId = W_DBObject.state.id_counter
        W_DBObject.state.id_counter += 1
        return theId

    def __init__(self, space, w_class, size, weak=False, w_id=None,
                 cache=None):
        W_PointersObject.__init__(self, space, w_class, size, weak)
        self.ivar_cache = cache if cache else [None] * size
        if w_id is not None:
            self.id = space.unwrap_int(w_id)
            self.w_id = w_id
            return
        self.id = W_DBObject.next_id()
        self.w_id = space.wrap_int(self.id)
        class_name = self.class_name(space)
        W_DBObject.state.init_column_types_if_neccessary(class_name, size)
        connection = dbm.connection()
        W_DBObject.state.create_table_if_neccessary(space, class_name,
                                                    connection)
        connection.execute(space, insert_sql(class_name), [self.w_id])

    def class_name(self, space):
        return jit.promote_string(self.classname(space))

    def is_same_object(self, other):
        if not isinstance(other, W_DBObject):
            return False
        # IDs are unique at the moment, so no need to check class_name as well
        return self.id == other.id

    def fetch(self, space, n0):
        class_name = self.class_name(space)
        if W_DBObject.state.get_column_type(class_name, n0) is NIL:
            # print "Can't find column. Falling back to default fetch."
            return W_PointersObject.fetch(self, space, n0)

        if self.ivar_cache[n0] is not None:
            return self.ivar_cache[n0]

        cursor = dbm.connection().execute(
            space, select_sql(class_name, n0), [self.w_id])

        w_result = cursor.next(space).fetch(space, 0)
        if w_result:
            if W_DBObject.state.get_column_type(class_name, n0) is BLOB:
                db_id = space.unwrap_int(w_result)
                w_result = W_DBObject.state.db_objects[db_id]

            self.ivar_cache[n0] = w_result
            return w_result
        else:
            raise PrimitiveFailedError

    def store(self, space, n0, w_value):
        self.ivar_cache[n0] = w_value

        cls = w_value.getclass(space)
        if (cls.is_same_object(space.w_String)):
            aType = TEXT
        elif cls.is_same_object(space.w_SmallInteger):
            aType = INTEGER
        elif cls.is_same_object(space.w_Float):
            aType = REAL
        elif cls.is_same_object(space.w_nil):
            aType = NIL
        else:
            if isinstance(w_value, W_DBObject):
                aType = BLOB
                W_DBObject.state.db_objects[w_value.id] = w_value
                # Save id in database.
                w_value = w_value.w_id
            else:
                # print 'Unable to unwrap %s' % w_value.getclass(space)
                # print 'Falling back to standard store.'
                return W_PointersObject.store(self, space, n0, w_value)

        aType = jit.promote(aType)
        class_name = self.class_name(space)

        if (aType is not NIL and
                W_DBObject.state.get_column_type(class_name, n0) is NIL):
            connection = dbm.connection()
            connection.execute(space, alter_sql(class_name, n0, aType))
            # print "invalidate cache"
            connection.statement_cache.invalidate()
            W_DBObject.state.set_column_type(class_name, n0, aType)

        connection = dbm.connection()
        connection.execute(space, update_sql(class_name, n0),
                           [w_value, self.w_id])
