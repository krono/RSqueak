from spyvm import model, shadow

from rpython.rlib import rerased
from rpython.rlib import objectmodel, jit, signature
from rpython.rlib.listsort import TimSort
from rpython.rlib.objectmodel import import_from_mixin

# Disables all optimized strategies, for debugging.
only_list_storage = False

class AbstractStorageStrategy(object):
    _immutable_fields_ = []
    _attrs_ = []
    _settled_ = True
    strategy_tag = 'abstract'
    
    def __init__(self):
        pass
    def needs_objspace(self):
        # Return True, if fetch/store operations use the space parameter.
        # If not, the space-parameter can be passed in as None (probably).
        return False
    
    def fetch(self, space, w_obj, n0):
        raise NotImplementedError("Abstract base class")
    def store(self, space, w_obj, n0, w_val):
        raise NotImplementedError("Abstract base class")
    def size_of(self, w_obj):
        raise NotImplementedError("Abstract base class")
    
    def initial_storage(self, space, size):
        raise NotImplementedError("Abstract base class")
    def storage_for_list(self, space, collection):
        raise NotImplementedError("Abstract base class")
    def copy_storage_from(self, space, w_obj, reuse_storage=False):
        old_strategy = w_obj.strategy
        if old_strategy == self and reuse_storage:
            return w_obj.get_storage(self)
        if isinstance(old_strategy, AllNilStorageStrategy):
            return self.initial_storage(space, old_strategy.size_of(w_obj))
        else:
            # This can be overridden and optimized (reuse_storage flag, less temporary storage)
            return self.storage_for_list(space, w_obj.fetch_all(space))

class SingletonMeta(type):
    def __new__(cls, name, bases, dct):
        result = type.__new__(cls, name, bases, dct)
        result.singleton = result()
        return result

# This is a container for an int-value to be used with a rerased-pair
class SizeStorage(object):
    _attrs_ = ['size']
    _settled_ = True
    def __init__(self, size):
        self.size = size

# this is the typical "initial" storage strategy, for when every slot
# in a var-sized object is still nil. No storage is allocated except for
# holding the size of the object.
class AllNilStorageStrategy(AbstractStorageStrategy):
    __metaclass__ = SingletonMeta
    erase, unerase = rerased.new_erasing_pair("all-nil-strategy")
    erase = staticmethod(erase)
    unerase = staticmethod(unerase)
    strategy_tag = 'allnil'
    
    def fetch(self, space, w_obj, n0):
        return model.w_nil
    
    def store(self, space, w_obj, n0, w_val):
        # This is an important moment, where we decide where to go on the first non-nil store.
        if w_val == model.w_nil:
            return
        if not only_list_storage:
            if isinstance(w_val, model.W_SmallInteger):
                return w_obj.store_with_new_strategy(space, DenseSmallIntegerStorageStrategy.singleton, n0, w_val)
        return w_obj.store_with_new_strategy(space, ListStorageStrategy.singleton, n0, w_val)
        
    def size_of(self, w_obj):
        return self.unerase(w_obj.get_storage(self)).size
    def initial_storage(self, space, size):
        return self.erase(SizeStorage(size))
    def storage_for_list(self, space, collection):
        return self.erase(SizeStorage(len(collection)))
    def copy_storage_from(self, space, w_obj, reuse_storage=False):
        return self.erase(SizeStorage(w_obj.basic_size()))

# This is the regular storage strategy that does not result in any
# optimizations but can handle every case. Applicable for both
# fixed-sized and var-sized objects.
class ListStorageStrategy(AbstractStorageStrategy):
    __metaclass__ = SingletonMeta
    erase, unerase = rerased.new_erasing_pair("list-storage-strategy")
    erase = staticmethod(erase)
    unerase = staticmethod(unerase)
    strategy_tag = 'list'
    
    def get_list(self, w_obj):
        return self.unerase(w_obj.get_storage(self))
    def fetch(self, space, w_obj, n0):
        return self.get_list(w_obj)[n0]
    def store(self, space, w_obj, n0, w_val):
        # TODO enable generalization by maintaining a counter of elements that are nil.
        self.get_list(w_obj)[n0] = w_val
    def size_of(self, w_obj):
        return len(self.get_list(w_obj))
    def initial_storage(self, space, size):
        return self.erase([model.w_nil] * size)
    def storage_for_list(self, space, collection):
        return self.erase([x for x in collection])
    def copy_storage_from(self, space, w_obj, reuse_storage=False):
        length = w_obj.basic_size()
        return self.erase([w_obj.strategy.fetch(space, w_obj, i) for i in range(length)])

class BasicStorageStrategyMixin(object):
    # Concrete class must implement: unwrap, unerase
    # Concrete class must provide attribute: contained_type (or override can_contin_object)
    
    def _storage(self, w_obj):
        return self.unerase(w_obj.get_storage(self))
    def do_fetch(self, space, arr, n0):
        # Default: access single unwrapped object at this location
        return self.wrap(space, arr[n0])
    def do_store(self, space, arr, n0, val):
        # Default implementation: store the single unwrapped object at this position.
        arr[n0] = self.unwrap(space, val)
    def size_of(self, w_obj):
        return len(self._storage(w_obj).arr) / self.slots_per_object(w_obj)
    def slots_per_object(self, w_obj):
        # Default: Each object is stored in a single slot.
        return 1
    def can_contain_object(self, w_val):
        return isinstance(w_val, self.contained_type)

class DenseStorage(object):
    # Concrete class must provide attribute: default_element
    _immutable_fields_ = ['arr']
    _attrs_ = ['arr', '_from', '_to']
    _settled_ = True
    
    def __init__(self, _from, _to, size):
        self._from = _from # first used index ("inclusive")
        self._to = _to # first unused index ("exclusive")
        self.arr = [self.default_element] * size

class DenseStorageStrategyMixin(object):
    # Concrete class must implement: erase, unerase, wrap, unwrap, sparse_strategy
    # Concrete class must provide attributes: contained_type, storage_type
    
    def fetch(self, space, w_obj, n0):
        store = self._storage(w_obj)
        if n0 < store._from or n0 >= store._to:
            return model.w_nil
        return self.do_fetch(space, store.arr, n0)
    def store(self, space, w_obj, n0, w_val):
        store = self._storage(w_obj)
        if not self.can_contain_object(w_val):
            if w_val == model.w_nil:
                if store._to - 1 == n0: # Optimize Collection >> remove:
                    store._to = store._to - 1
                elif n0 < store._from or store._to <= n0:
                    pass # Storing nil to an already-nil position
                elif store._from == n0:
                    store._from = store._from + 1
                else:
                    # Deletion from the middle of the storage. Deoptimize to sparse storage.
                    return w_obj.store_with_new_strategy(space, self.sparse_strategy().singleton, n0, w_val)
                if store._from == store._to:
                    # Deleted last element. Generelize to AllNilStorage.
                    w_obj.switch_strategy(space, AllNilStorageStrategy.singleton)
                return
            else:
                # Storing a non-int - dehomogenize to ListStorage
                return w_obj.store_with_new_strategy(space, ListStorageStrategy.singleton, n0, w_val)
        if n0 == store._to: # Optimize Collection >> add:
            store._to = store._to+1
        elif store._from <= n0 and n0 < store._to:
            pass
        elif n0 == store._from - 1: # It's ok if this wraps around.
            store._from = store._from-1
        else:
            if store._from == store._to:
                # Initial store to non-zero position.
                store._from = n0
                store._to = n0+1
            else:
                # Store to a non-dense position. Deoptimize to sparse storage.
                return w_obj.store_with_new_strategy(space, self.sparse_strategy().singleton, n0, w_val)
        # It is a dense store, so finally store the unwrapped value.
        self.do_store(space, store.arr, n0, w_val)
    def initial_storage(self, space, size):
        return self.erase(self.storage_type(0, 0, size))
    def storage_for_list(self, space, collection):
        _from = 0
        while _from < len(collection) and collection[_from] == model.w_nil:
            _from = _from+1
        _to = _from
        while _to < len(collection) and collection[_to] != model.w_nil:
            _to = _to+1
        store = self.storage_type(_from, _to, len(collection))
        for i in range(_from, _to):
            self.do_store(space, store.arr, i, collection[i])
        return self.erase(store)

class SparseStorage(object):
    _immutable_fields_ = ['arr', 'nil_flags']
    _attrs_ = ['arr', 'nil_flags']
    _settled_ = True
    
    def __init__(self, arr, nil_flags):
        self.arr = arr
        self.nil_flags = nil_flags
    
class SparseStorageStrategyMixin(object):
    # Concrete class must implement: erase, unerase, wrap, unwrap, dense_strategy
    # Concrete class must provide attributes: contained_type, storage_type
    
    def fetch(self, space, w_obj, n0):
        store = self._storage(w_obj)
        if store.nil_flags[n0]:
            return model.w_nil
        return self.do_fetch(space, store.arr, n0)
    def store(self, space, w_obj, n0, w_val):
        store = self._storage(w_obj)
        if not self.can_contain_object(w_val):
            if w_val == model.w_nil:
                # TODO - generelize to AllNilStorage by maintaining a counter of nil-elements
                store.nil_flags[n0] = True
                return
            else:
                # Storing a wrong type - dehomogenize to ListStorage
                return w_obj.store_with_new_strategy(space, ListStorageStrategy.singleton, n0, w_val)
        store.nil_flags[n0] = False
        self.do_store(space, store.arr, n0, w_val)
    def storage_for_size(self, size):
        # TODO -- for inlining strategy, the size must be extended!!
        # size = size * self.slots_per_object()
        return self.storage_type([self.storage_type.default_element] * size, [True] * size)
    def initial_storage(self, space, size):
        return self.erase(self.storage_for_size(size))
    def storage_for_list(self, space, collection):
        length = len(collection)
        store = self.storage_for_size(length)
        for i in range(length):
            if collection[i] != model.w_nil:
                store.nil_flags[i] = False
                self.do_store(space, store.arr, i, collection[i])
        return self.erase(store)
    def copy_storage_from(self, space, w_obj, reuse_storage=False):
        old_strategy = w_obj.strategy
        if isinstance(old_strategy, self.dense_strategy()):
            # Optimized transition from dense to sparse strategy
            store = old_strategy._storage(w_obj)
            return self.erase(self.copy_from_dense_storage(store, reuse_storage))
        else:
            return AbstractStorageStrategy.copy_storage_from(self, space, w_obj, reuse_storage)
    def copy_from_dense_storage(self, store, reuse_storage):
        # TODO possible optimization: compare len(arr) with _to-_from, use smaller iteration size
        nil_flags = [True] * len(store.arr)
        for i in range(store._from, store._to):
            nil_flags[i] = False
        arr = store.arr
        if not reuse_storage:
            arr = [x for x in arr]
        return self.storage_type(arr, nil_flags)
    
class SmallIntegerStorageStrategyMixin(object):
    contained_type = model.W_SmallInteger
    def needs_objspace(self):
        return True
    def wrap(self, space, obj):
        return space.wrap_int(obj)
    def unwrap(self, space, w_obj):
        return space.unwrap_int(w_obj)
    
class SparseSmallIntegerStorage(SparseStorage):
    default_element = 0

class SparseSmallIntegerStorageStrategy(AbstractStorageStrategy):
    __metaclass__ = SingletonMeta
    import_from_mixin(BasicStorageStrategyMixin)
    import_from_mixin(SparseStorageStrategyMixin)
    import_from_mixin(SmallIntegerStorageStrategyMixin)
    erase, unerase = rerased.new_erasing_pair("sparse-small-integer-strategry")
    erase = staticmethod(erase)
    unerase = staticmethod(unerase)
    strategy_tag = 'sparse-small-int'
    storage_type = SparseSmallIntegerStorage
    def dense_strategy(self):
        return DenseSmallIntegerStorageStrategy
    
class DenseSmallIntegerStorage(DenseStorage):
    default_element = 0

class DenseSmallIntegerStorageStrategy(AbstractStorageStrategy):
    __metaclass__ = SingletonMeta
    import_from_mixin(BasicStorageStrategyMixin)
    import_from_mixin(DenseStorageStrategyMixin)
    import_from_mixin(SmallIntegerStorageStrategyMixin)
    erase, unerase = rerased.new_erasing_pair("dense-small-integer-strategry")
    erase = staticmethod(erase)
    unerase = staticmethod(unerase)
    strategy_tag = 'dense-small-int'
    storage_type = DenseSmallIntegerStorage
    def sparse_strategy(self):
        return SparseSmallIntegerStorageStrategy

class InliningListStorageStrategyMixin(object):
    #.
    
    def _storage(self, w_obj):
        return self.unerase(w_obj.get_storage(self))
        
    def needs_objspace(self):
        return True
    
    @staticmethod
    def is_inlineable_object(space, w_obj):
        return (isinstance(w_obj, W_PointersObject) and
            not w_obj.s_class.isvariable() and
            InliningListStrategyMixin.has_only_smallint_fields(space, w_obj))
            
    @staticmethod
    def has_only_smallint_fields(space, w_obj):
        for i in range(0, self._storage(w_obj).s_inlined_class._instance_size):
            if not isinstance(w_obj.fetch(space, i), W_SmallInteger):
                return False
        return True
    
    def can_contain_object(self, w_obj):
        return (isinstance(w_obj, W_PointersObject) and
            w_obj.s_class == self._storage(w_obj).s_inlined_class and
            InliningListStrategyMixin.has_only_smallint_fields(space, w_obj))
    
    def do_fetch(self, space, w_container_obj, n0):
        store = self._storage(w_obj)
        w_referencing_obj = W_PointersObject(space, store.s_inlined_class, 0)
        w_referencing_obj.hash = store.hashes[n0]
        # The referencing object should never get a shadow!
        # TODO - probably create special subclass of AbstractObjectWithIdentityHash
        self.make_referencing_object(w_referencing_obj, w_container_obj, n0)
        return w_referencing_obj
    
    def make_referencing_object(self, w_referencing_obj, w_container_obj, n0):
        # TODO -- make a complete copy of the object, if we assume read-only objects.
        w_referencing_obj.switch_strategy(InlinedObjectStrategy.singleton)
        InlinedObjectStrategy.singleton.set_object_storage(w_referencing_obj, w_container_obj, n0)
    
    def do_store(self, space, w_obj, n0, w_val):
        if self.fetch(space, w_obj, n0) != model.w_nil:
            # An object was already stored here -> deoptimize.
            self.deoptimize(space, w_obj)
            return w_obj.store(space, n0, w_val)
        store = self._storage(w_obj)
        store.hashes[n0] = w_val.gethash()
        for i in range(0, store.s_contained_class._instance_size):
            store.arr[n0 + i] = space.unwrap_int(w_val.fetch(space, i))
        # The input object itself must be converted to be a reference inside this list.
        # TODO -- don't do this if we assume read-only objects.
        self.make_referencing_object(w_val, w_obj, n0)
        
    def size_of(self, w_obj):
        return (len(self._storage(w_obj).arr) / 
                self._storage(w_obj).s_inlined_class._instance_size)
    
    def get_inlined_field(self, space, w_obj, object_index, field_index):
        store = self._storage(w_obj)
        return space.wrap_int(store.arr[object_index + field_index])
    
    def put_inlined_field(self, space, w_obj, object_index, field_index, w_val):
        store = self._storage(w_obj)
        store.arr[object_index + field_index] = space.unwrap_int(w_val)
    
class AbstractInliningListStorageStrategy(AbstractStorageStrategy):
    # The two inlining storage types need a joined super class.
    def get_inlined_field(self, space, w_obj, object_index, field_index):
        raise NotImplementedError("Abstract base class")
    def put_inlined_field(self, space, w_obj, object_index, field_index, w_val):
        raise NotImplementedError("Abstract base class")
    def deoptimize(self, space, w_obj):
        # TODO -- Create special strategy that holds the old 
        # list (to maintain objects with an InlinedObjectStrategy) and
        # a new list that contains regular, deoptimized copies of the formerly inlined objects.
        # The old list will be used by get_inlined_field/put_inlined_field,
        # the new list will be used by fetch/store
        raise NotImplementedError("TODO")

class DenseInliningStorage(DenseStorage):
    default_element = 0
    def __init__(self, _from, _to, size):
        DenseStorage.__init__(self, _from, _to, size)
        self.hashes = [0] * size
        self.s_inlined_class = None

class DenseInliningListStorageStrategy(AbstractInliningListStorageStrategy):
    __metaclass__ = SingletonMeta
    import_from_mixin(DenseStorageStrategyMixin)
    import_from_mixin(InliningListStorageStrategyMixin)
    erase, unerase = rerased.new_erasing_pair("dense-inlining-list-storage-strategy")
    erase = staticmethod(erase)
    unerase = staticmethod(unerase)
    strategy_tag = 'dense-inlining-list'
    storage_type = DenseInliningStorage
    def sparse_strategy():
        return SparseInliningListStorageStrategy

class SparseInliningStorage(SparseStorage):
    default_element = 0
    def __init__(self, arr, nil_flags):
        SparseStorage.__init__(self, arr, nil_flags)
        self.hashes = [0] * size
        self.s_inlined_class = None

class SparseInliningListStorageStrategy(AbstractInliningListStorageStrategy):
    __metaclass__ = SingletonMeta
    import_from_mixin(SparseStorageStrategyMixin)
    import_from_mixin(InliningListStorageStrategyMixin)
    erase, unerase = rerased.new_erasing_pair("dense-inlining-list-storage-strategy")
    erase = staticmethod(erase)
    unerase = staticmethod(unerase)
    strategy_tag = 'dense-inlining-list'
    storage_type = SparseInliningStorage
    def sparse_strategy():
        return SparseInliningListStorageStrategy
    def copy_from_dense_storage(self, store, reuse_storage):
        new_store = SparseStorageStrategyMixin.copy_from_dense_storage(self, store, reuse_storage)
        hashes = store.hashes
        if not reuse_storage:
            hashes = [x for x in hashes]
        new_store.hashes = hashes
        new_store.s_inlined_class = store.s_inlined_class
        return new_store

class InlinedObjectStorage(object):
    attrs = ['w_referenced', 'n0']
    def __init__(self, w_referenced, n0):
        self.w_referenced = w_referenced
        self.n0 = n0

class InlinedObjectStrategy(AbstractStorageStrategy):
    __metaclass__ = SingletonMeta
    erase, unerase = rerased.new_erasing_pair("inlined-object-storage-strategy")
    erase = staticmethod(erase)
    unerase = staticmethod(unerase)
    strategy_tag = 'inlined object'
    
    def fetch(self, space, w_obj, n0):
        my_store = self._storage(w_obj)
        w_referenced = my_store.w_referenced
        return w_referenced.storage.get_inlined_field(space, w_referenced, my_store.n0, n0)
        
    def store(self, space, w_obj, n0, w_val):
        my_store = self._storage(w_obj)
        w_referenced = my_store.w_referenced
        if not isinstance(w_val, model.W_SmallInteger):
            # The deoptimization happens in-place. put_inlined_field() can still be called
            # after the deoptimization.
            w_referenced.storage.deoptimize(space, w_referenced)
        w_referenced.storage.put_inlined_field(space, w_referenced, my_store.n0, n0, w_val)
        
    def set_object_storage(self, w_referencing_obj, w_container_obj, n0):
        store = self._storage(w_referencing_obj)
        store.w_referenced = w_container_obj
        store.n0 = n0
    
    def size_of(self, w_obj):
        store = self._storage(w_obj)
        referenced_store = store.w_referenced.strategy._storage(store.w_referenced)
        return referenced_store.s_inlined_class._instance_size
    def initial_storage(self, space, size):
        return self.erase(InlinedObjectStorage(None, 0))
    def storage_for_list(self, space, collection):
        # TODO -- make sure this is true.
        raise NotImplementedException("An inlined object cannot exist at image startup")
    def copy_storage_from(self, space, w_obj, reuse_storage=False):
        # The storage of an inlined object reference will be initialized explicitely
        # by calling set_object_storage()
        pass

class TypeTag():
    pass

LPI = TypeTag()
SInt = TypeTag()
flt = TypeTag()
obj = TypeTag()

# may be used during debugging
# LPI, SInt, flt, obj = 'LPI', 'SInt', 'float', 'object'

class FieldSort(TimSort):
    def lt(self, a, b):
        return a[0] < b[0]

class FixedSizeFieldTypes(AbstractStorageStrategy):
    _immutable_fields_ = ['types[*]']
    _attrs_ = ['types', 'parent', 'siblings', 'diff']
    _settled_ = True
    
    erase, unerase = rerased.new_erasing_pair("fixed-size-field-types")
    erase = staticmethod(erase)
    unerase = staticmethod(unerase)
    strategy_tag = 'fixed'
    
    def __init__(self, types, parent=None, change=(-1, obj)):
        self.types = types
        self.parent = parent
        if parent is not None:
            assert change != (-1, obj)
        self.diff = change
        self.siblings = {}
    
    def initial_storage(self, space, size):
        return self.erase([model.w_nil] * size)
    
    def storage_for_list(self, space, collection):
        return self.erase([x for x in collection])
    
    def size_of(self, w_obj):
        return len(self.unerase(w_obj.get_storage(self)))
    
    def fetch(self, space, w_object, n0):
        w_result = self.unerase(w_object.get_storage(self))[n0]
        assert w_result is not None
        types = self.types
        # TODO - try 'assert isinstance' instead.
        if types[n0] is SInt:
            jit.record_known_class(w_result, model.W_SmallInteger)
        elif types[n0] is LPI:
            jit.record_known_class(w_result, model.W_LargePositiveInteger1Word)
        elif types[n0] is flt:
            jit.record_known_class(w_result, model.W_Float)
        return w_result

    def store(self, space, w_object, n0, w_value):
        types = self.types
        changed_type = w_value.fieldtype()
        if types[n0] is not changed_type:
            w_object.strategy = self.sibling(n0, changed_type)
        self.unerase(w_object.get_storage(self))[n0] = w_value

    @jit.elidable
    def sibling(self, n0, changed_type):
        assert self.types[n0] is not changed_type
        change = (n0, changed_type)
        parent = self.parent
        siblings = self.siblings
        if change in siblings:
            return siblings[change]
        elif parent is None:
            return self.descent([change])
        else:
            if n0 == self.diff[0]:
                diff = [change]
            else:
                diff = [change, self.diff]

            new_fieldtype = parent.ascent(diff)

            if not objectmodel.we_are_translated():
                new_types = list(self.types)
                new_types[n0] = changed_type
                assert new_fieldtype.types == new_types
            siblings[change] = new_fieldtype
            return new_fieldtype

    def ascent(self, changes):
        parent = self.parent
        if parent is None:
            FieldSort(changes).sort()
            return self.descent(changes)
        else:
            change = self.diff
            if changes[0][0] != change[0]:
                changes.append(change)
            return parent.ascent(changes)

    def descent(self, changes):
        if changes == []:
            return self

        change = changes[0]
        if change[1] is obj:
            return self.descent(changes[1:])
        siblings = self.siblings
        if change in siblings:
            return siblings[change].descent(changes[1:])
        else:
            new_types = list(self.types)
            assert new_types[change[0]] == obj
            new_types[change[0]] = change[1]
            new_fieldtype = FixedSizeFieldTypes(new_types, self, change)
            siblings[change] = new_fieldtype
            return new_fieldtype.descent(changes[1:])

    @staticmethod
    @jit.elidable
    def of_size(n):
        if n not in maps:
            maps[n] = FixedSizeFieldTypes([obj] * n)
        return maps[n]

maps = {}

def strategy_of_size(s_containing_class, size):
    if s_containing_class is None:
        # This is a weird and rare special case for w_nil
        return ListStorageStrategy.singleton
    if s_containing_class.isvariable():
        if only_list_storage:
            return ListStorageStrategy.singleton
        
        # A newly allocated var-sized object contains only nils.
        return AllNilStorageStrategy.singleton
    else:
        # TODO -- use strategy-concept for fixed-size classes also.
        # TODO -- monitor classes and create heuristic-based default strategies 
        # that should be more suited. Example: 2 small-integers for Point.
        return FixedSizeFieldTypes.of_size(size)

def strategy_for_list(s_containing_class, vars):
    if s_containing_class is None:
            # This is a weird and rare special case for w_nil
            return ListStorageStrategy.singleton
    try:
        is_variable = s_containing_class.isvariable()
    except AttributeError:
        # TODO - This happens during bootstrapping phase, when filling in generic objects.
        # Ths class object shadows are not yet synchronized.
        return ListStorageStrategy.singleton
    if is_variable:
        if only_list_storage:
            return ListStorageStrategy.singleton
        
        is_all_nils = True
        is_dense = True
        for w_obj in vars:
            if w_obj == model.w_nil:
                if not is_all_nils:
                    is_dense = False
            else:
                is_all_nils = False
                if not isinstance(w_obj, model.W_SmallInteger):
                    # TODO -- here we can still optimize if there is only
                    # one single type in the collection.
                    return ListStorageStrategy.singleton
        if is_all_nils:
            return AllNilStorageStrategy.singleton
        if is_dense:
            return DenseSmallIntegerStorageStrategy.singleton
        else:
            return SparseSmallIntegerStorageStrategy.singleton
    else:
        size = len(vars)
        typer = FixedSizeFieldTypes.of_size(size)
        for i, w_val in enumerate(vars):
            changed_type = w_val.fieldtype()
            if changed_type is not obj:
                typer = typer.sibling(i, changed_type)
        return typer
    