"""A module for dynamic, incremental ctypes code generation.

See the docstring of the update_from function for usage information.
"""
import sys, os, time, bz2, cPickle, md5
import distutils.dep_util
import ctypes
import ctypeslib
from ctypeslib.codegen import gccxmlparser, codegenerator, typedesc

# TODO:
#
# Clean up the names Generator and CodeGenerator.
#
# If the .xml file does not exist but the .xml.pck.bz2 file does,
# accept the latter.
#
# In include(): Something similar.

def update_from(xml_file, persist=True, _stacklevel=1):
    """This function replaces the *calling module* with a dynamic
    module that generates code on demand from type descriptions
    contained in the <xml_file>.  If <xml_file> is a relative
    pathname, it is interpreted relative to the calling modules
    __file__.

    If <persist> is True, generated code is appended to the module's
    source code, otherwise the generated code is executed and then
    thrown away.

    The calling module must load all the shared libraries that it uses
    *BEFORE* this function is called.

    BUGS:
     - the calling module MUST contain 'from ctypes import *',
       and, on windows, also 'from ctypes.wintypes import *'.
    """
    frame = sys._getframe(_stacklevel)
    glob = frame.f_globals
    name = glob["__name__"]
    mod = sys.modules[name]
    if not os.path.isabs(xml_file):
        xml_file = os.path.join(os.path.dirname(mod.__file__), xml_file)
    sys.modules[name] = DynamicModule(mod, xml_file, persist=persist)

def include(code):
    """Does the same as update_from above, but takes C code instead of
    an xml_file.  gccxml is used to create the xml_file with type
    descriptions in a 'cache' directory.
    """
    # create a hash for the code, and use that as basename for the
    # files we have to create
    hashval = md5.new(code).hexdigest()

    # XXX fixme: use fixed path
    fnm = os.path.abspath(os.path.join(os.path.dirname(__file__), "xml", hashval))

    h_file = fnm + ".h"
    xml_file = fnm + ".xml"

    if not os.path.exists(h_file):
        print "Create %s" % h_file
        open(h_file, "w").write(code)
    if distutils.dep_util.newer(h_file, xml_file):
        print "Create %s" % xml_file
        from ctypeslib import h2xml
        h2xml.main(["h2xml",
                    "-I", os.path.dirname(fnm), "-c", "-q",
                    h_file,
                    "-o", xml_file])
    update_from(xml_file, _stacklevel=2)
        
    

################################################################

class DynamicModule(object):
    def __init__(self, mod, xml_file, persist):
        # We need to keep 'mod' alive, otherwise it would set the
        # values of it's __dict__ to None when it's deleted.
        self.__dict__ = mod.__dict__
        self.__orig_module__ = mod
        fnm = os.path.abspath(self.__file__)
        if fnm.endswith(".pyc") or fnm.endswith(".pyo"):
            fnm = fnm[:-1]
        if persist and not os.path.exists(fnm):
            raise ValueError("source file %r does not exist" % fnm)
        self.__generator = CodeGenerator(fnm, xml_file, mod.__dict__, persist)
        self.__xml_file = xml_file

    def __repr__(self):
        return "<DynamicModule(%r) %r from %r>" % (self.__xml_file, self.__name__, self.__file__)

    def __getattr__(self, name):
        if not name.startswith("__") and not name.endswith("__"):
            val = self.__generator.generate(name)
            self.__dict__[name] = val
            return val
        raise AttributeError(name)

################

class UnknownSymbol(Exception):
    pass

class Generator(codegenerator.Generator):
    """A subclass of codegenerator, specialized for our requirements:

    - libraries are already loaded in the module, won't be loaded by
    the code we generate.

    - no need to generate symbols that are already present in
    self.namespace
    """

    def need_CLibraries(self): pass
    # Libraries are already loaded in the module, no code needed
    need_WinLibraries = need_CLibraries

    def generate(self, item):
        if isinstance(item, typedesc.StructureHead):
            name = getattr(item.struct, "name", None)
        else:
            name = getattr(item, "name", None)
        if name in self.namespace:
            return
        super(Generator, self).generate(item)
        

    def get_sharedlib(self, dllname, cc):
        # XXX This should assert that the correct calling convention
        # is used.
        dll = self.searched_dlls[dllname]
        if cc == "stdcall":
            assert isinstance(dll, ctypes.WinDLL), "wrong calling convention"
        else:
            assert not isinstance(dll, ctypes.WinDLL), "wrong calling convention"
        return dllname

    def find_dllname(self, func):
        # Find which of the libraries in 'searched_dlls' exports the
        # function 'func'.  Return name of library or None.
        name = func.name
        for dllname, dll in self.searched_dlls.items():
            try:
                getattr(dll, name)
            except AttributeError:
                pass
            else:
                return dllname
        return None


    def Function(self, func):
        # XXX Not sure this is approach makes sense.
        super(Generator, self).Function(func)
        restype = self.type_name(func.returns)
        errcheck = self.namespace.get("%s_errcheck" % restype, None)
        if errcheck is not None:
            print >> self.stream, "%s.errcheck = %s_errcheck" % (func.name, restype)

class CodeGenerator(object):
    """Dynamic, incremental code generation.  The generated code is
    executed in the dictionary <ns>, and appended to the file
    specified by <src_path>, if <persist> is True."""
    output = None
    def __init__(self, src_path, xml_file, ns, persist):
        # We should do lazy initialization, so that all this stuff is
        # only done when really needed because we have to generate
        # something.
        start = time.clock()
        if persist:
            # We open the file in universal newline mode, read the
            # whole contents and write it back to avoid creating a
            # file with multiple line endings.
            data = open(src_path, "U").read()
            self.output = open(src_path, "w")
            self.output.write(data)
        pck_file = xml_file + ".pck.bz2"
        if distutils.dep_util.newer(xml_file, pck_file):
            decls = gccxmlparser.parse(xml_file)
##            print "parsing xml took %.2f seconds" % (time.clock() - start)
            start = time.clock()
            ofi = bz2.BZ2File(pck_file, "w")
            data = cPickle.dump(decls, ofi, -1)
##            print "dumping .pck took %.2f seconds" % (time.clock() - start)
        else:
            data = open(pck_file, "rb").read()
            decls = cPickle.loads(bz2.decompress(data))
##            print "loading .pck took %.2f seconds" % (time.clock() - start)
        names = {}
        self.namespace = ns
        done = set()
        for i in decls:
            try:
                name = i.name
            except AttributeError:
                continue
            if name in ns:
                done.add(i)
                if isinstance(i, typedesc.Structure):
                    done.add(i.get_head())
                    done.add(i.get_body())
            names[name] = i
        self.decls = names

        dlls = dict([o for o in ns.items()
                     if isinstance(o[1], ctypes.CDLL)
                     and not isinstance(o[1], ctypes.PyDLL)])

        self.codegenerator = Generator(output=None,
                                       known_symbols=None,
                                       searched_dlls=dlls)
        self.codegenerator.errcheck = ns.get("errcheck")
        self.codegenerator.done |= done
        self.codegenerator.namespace = self.namespace

        self.imports = ""
        self.code = ""

    def generate(self, name):
        # Incremental code generation for one name.
        try:
            item = self.decls[name]
        except KeyError:
            raise UnknownSymbol(name)
        self.codegenerator.generate_items([item])

        # Could as well call getvalue(), and create a new StringIO
        # instance for .imports and .stream.
        imports = self.codegenerator.imports.getvalue()[len(self.imports):]
        self.imports += imports
        code = self.codegenerator.stream.getvalue()[len(self.code):]
        self.code += code

        code = imports + code

        exec code in self.namespace
        # I guess when this fails, it means that the dll exporting
        # this function is not in searched_dlls.  So we should
        # probably raise a different exception:
##        print code
        if self.output is not None:
            self.output.write(code)
        try:
            return self.namespace[name]
        except KeyError:
            raise UnknownSymbol(name)

################################################################
