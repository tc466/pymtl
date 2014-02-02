#!/usr/bin/env python
#==============================================================================
# Verilate and Cythonize Translated PyMTL Models
#==============================================================================

import sys
import re
import os
import math
import textwrap

import verilog_structural

from subprocess import check_output, STDOUT, CalledProcessError

#------------------------------------------------------------------------------
# verilog_to_pymtl
#------------------------------------------------------------------------------
# Create a PyMTL compatible interface for Verilog HDL.
def verilog_to_pymtl( model, filename_v ):

  # TODO: clean this up
  if   isinstance( model, str ):
    model_name = model
  elif isinstance( model, type ):
    x = model()
    model_name = x.__class__.__name__
  else:
    model_name = model.class_name

  # Output file names
  filename_cpp = model_name + '.cpp'
  filename_w   = 'W' + model_name + '.py'
  vobj_name    = 'V' + model_name
  xobj_name    = 'X' + model_name

  # Verilate the model
  # TODO: clean this up
  verilate_model( filename_v, model_name )

  # Get the ports of the module
  in_ports, out_ports = get_model_ports( model )

  # Create C++ Wrapper
  cdefs = create_c_wrapper( in_ports, out_ports, model_name, filename_cpp )

  # Create Shared C Library
  create_shared_lib( model_name )

  # Create PyMTL wrapper for CFFI interface to Verilated model
  create_pymtl_wrapper( in_ports, out_ports, model_name,
                        filename_w, vobj_name, xobj_name, cdefs )

#------------------------------------------------------------------------------
# verilate_model
#------------------------------------------------------------------------------
# Convert Verilog HDL into a C++ simulator using Verilator.
# http://www.veripool.org/wiki/verilator
def verilate_model( filename, model_name ):
  # Verilate the translated module (warnings suppressed)
  cmd = 'rm -r obj_dir_{1}; verilator -cc {0} -top-module {1}' \
        ' --Mdir obj_dir_{1} -trace -Wno-lint -Wno-UNOPTFLAT'   \
        .format( filename, model_name )
  print cmd
  os.system( cmd )

#------------------------------------------------------------------------------
# get_model_ports
#------------------------------------------------------------------------------
# Return all input and output ports of a PyMTL model.
def get_model_ports( model_name ):
  # TODO: clean this up, this is getting really messy...
  # Import the specified module
  # If we received a module name from the commandline, we need to import
  if isinstance( model_name, str ):
    __import__( model_name )
    imported_module = sys.modules[ model_name ]
    model_class = imported_module.__dict__[ model_name ]
    model_inst = model_class()
    model_inst.elaborate()
  # We received a model class definition (not an instance!)
  elif isinstance( model_name, type ):
    model_class = model_name
    model_inst = model_class()
    model_inst.elaborate()
  # Otherwise we received a model instance!
  else:
    model_inst = model_name

  # Collect the input/output ports
  in_ports  = model_inst.get_inports()
  out_ports = model_inst.get_outports()

  def verilog_name( port ):
    return verilog_structural.mangle_name( port.name )

  in_ports = [ ( verilog_name( p ), str( p.nbits ) )
               for p in in_ports
               if not verilog_name( p ) in [ 'clk', 'reset' ] ]
  out_ports = [ ( verilog_name( p ), str( p.nbits ) ) for p in out_ports ]

  return in_ports, out_ports

#------------------------------------------------------------------------------
# create_c_wrapper
#------------------------------------------------------------------------------
# Generate a C wrapper file for Verilated C++.
def create_c_wrapper( in_ports, out_ports, model_name, filename_cpp ):

  c_src = """
    #include "obj_dir_{model_name}/V{model_name}.h"
    #include "stdio.h"
    #include "stdint.h"
    #include "verilated.h"
    #include "verilated_vcd_c.h"

    extern "C" {{
      extern void create_model( void );
      extern void destroy_model( void );
      extern void eval( void );
      extern void trace( void );

      {port_externs}
    }}

    // Verilator models
    V{model_name} * model;
    VerilatedVcdC * tfp;
    unsigned int trace_time;

    // Interface port pointers exposed via CFFI
    {port_decls}

    // Constructor
    void create_model() {{
      model = new V{model_name}();
      tfp   = new VerilatedVcdC();

      // Enable tracing
      Verilated::traceEverOn( true );
      trace_time = 0;
      model->trace( tfp, 99 );
      tfp->open( "{model_name}.vcd" );

      // Initialize interface pointers
      {port_inits}
    }}

    // Destructor
    void destroy_model() {{
      delete model;
      tfp->close();
    }}

    void trace() {{
      trace_time += 1;
      tfp->dump( trace_time );
    }}

    void eval() {{
      model->eval();
    }}
  """

  # Collect all the ports
  ports = [ ('clk', '1'), ('reset', '1') ] + in_ports + out_ports

  # Utility function for creating port declarations
  def port_to_decl( port ):
    code = '{data_type} * {port_name};'

    port_name, bitwidth = port
    port_name = verilator_mangle( port_name )
    bitwidth = int( bitwidth )

    if   bitwidth <= 8:  data_type = 'uint8_t'
    elif bitwidth <= 16: data_type = 'uint16_t'
    elif bitwidth <= 32: data_type = 'uint32_t'
    elif bitwidth <= 64: data_type = 'uint64_t'
    else:                data_type = 'uint32_t'

    return code.format( **vars() )

  # Utility function for creating port initializations
  def port_to_init( port ):
    code = '{verilator_name} = {dereference}model->{verilator_name};'
    port_name, bitwidth = port
    dereference = '&' if int(bitwidth) <= 64 else ''
    # TODO: hack for verilator name mangling, make this cleaner?
    verilator_name = verilator_mangle( port_name )
    return code.format( port_name      = port_name,
                        dereference    = dereference,
                        verilator_name = verilator_name )

  # Create port declaration, initialization, and extern statements
  pfx = 'extern '
  port_externs = '\n      '.join( [ pfx+port_to_decl( x ) for x in ports ] )
  port_decls   = '\n    '  .join( [     port_to_decl( x ) for x in ports ] )
  port_inits   = '\n      '.join( [     port_to_init( x ) for x in ports ] )

  # Generate the source code using the template
  c_src = c_src.format( model_name   = model_name,
                        port_externs = port_externs,
                        port_decls   = port_decls,
                        port_inits   = port_inits,
                      )
  c_src = textwrap.dedent( c_src )

  # Write out the source code to files
  f = open( filename_cpp, 'w' )
  f.write( c_src )
  f.close()

  return port_decls

#------------------------------------------------------------------------------
# create_shared_lib
#------------------------------------------------------------------------------
# Compile the cpp wrapper into a shared library.
def create_shared_lib( model_name ):

  # Compiler template string
  compile_cmd  = 'g++ {flags} -I {verilator} -o {libname} {cpp_sources}'

  flags        = '-O3 -fPIC -shared'
  verilator    = '/Users/dmlockhart/vc/git-opensource/verilator/include'
  libname      = '{model_name}.so'
  cpp_sources  = ' '.join( [
                   'obj_dir_{model_name}/V{model_name}.cpp',
                   'obj_dir_{model_name}/V{model_name}__Syms.cpp',
                   'obj_dir_{model_name}/V{model_name}__Trace.cpp',
                   'obj_dir_{model_name}/V{model_name}__Trace__Slow.cpp',
                   '{verilator}/verilated.cpp',
                   '{verilator}/verilated_vcd_c.cpp',
                   '{model_name}.cpp'
                 ])

  # Substitute flags in compiler string, then any remaining flags
  compile_cmd = compile_cmd.format( **vars() )
  compile_cmd = compile_cmd.format( model_name = model_name,
                                    verilator  = verilator )

  # Perform compilation
  try:
    result = check_output( compile_cmd.split() , stderr=STDOUT )
  except CalledProcessError as e:
    error_msg = """
      Module did not compile!

      Command:
      {command}

      Error:
      {error}
    """

    # Source:
    # \x1b[31m {source} \x1b[0m

    raise Exception( error_msg.format(
      command = ' '.join( e.cmd ),
      error   = e.output
    ))

#------------------------------------------------------------------------------
# create_pymtl_wrapper
#------------------------------------------------------------------------------
# Create PyMTL wrapper for CFFI interface of Verilated model.
def create_pymtl_wrapper( in_ports, out_ports, model_name, filename_w,
                          vobj_name, xobj_name, cdefs ):

  py_src = """
    from new_pymtl import *
    from cffi      import FFI

    class {model_name}( Model ):

      def __init__( s ):

        ffi = FFI()
        ffi.cdef('''
          void create_model( void );
          void destroy_model( void );
          void eval( void );
          void trace( void );

          {port_decls}
        ''')

        s._model = ffi.dlopen('./{model_name}.so')
        s._model.create_model()

        {port_defs}

      def __del__( s ):
        s._model.destroy_model()

      def elaborate_logic( s ):
        @s.combinational
        def logic():

          # Set reset
          s._model.reset[0] = s.reset

          # Set inputs
          {set_inputs}

          # Execute combinational logic
          s._model.eval()

          # Set outputs
          {set_outputs}

        @s.posedge_clk
        def tick():

          # Tick and capture VCD output
          s._model.eval()
          s._model.trace()

          # Set clk high and repeat
          s._model.clk[0] = 1
          s._model.eval()
          s._model.trace()

          {set_next}

          s._model.clk[0] = 0

  """

  # TODO: handle PortBundles
  # Any signals with an _M_ in the name are part of bundles, handle these
  # specially by creating 'fake' PortBundle inner classes.
  #from collections import defaultdict
  #bundles = set()
  #for name, width in in_ports + out_ports:
  #  if '_M_' in name:
  #    bundle_name, port_name = name.split('_M_')
  #    bundles.add( bundle_name )
  #for b in bundles:
  #  w += ("    class {1}( PortBundle ): flip = False\n"
  #        "    s.{0} = {1}()\n\n".format( b, b.capitalize() ) )

  # Declare the interface ports for the wrapper class.
  port_defs = []
  for ports, port_type in [( in_ports, 'InPort' ), ( out_ports, 'OutPort' )]:

    # Replace all references to _M_ with .
    ports = [ ( name.replace('_M_', '.'), nbits ) for name, nbits in ports ]

    # TODO: handle port lists
    #s.{0} = [ {3}( {1} ) for x in range( {2} ) ]'
    #        '\n'.format( i[0], i[1], i[2], ptype ))

    lists = []
    for port_name, bitwidth in ports:
      # Port List
      if '$' in port_name:
        pfx = port_name.split('$')[0]
        if pfx not in lists:
          lists.append(pfx)
          port_defs.append( 's.{} = []'.format( pfx ) )
        port_defs.append( 's.{port_list}.append( {port_type}( {bitwidth} ) )' \
                          .format( port_list = pfx,
                                    port_type = port_type,
                                    bitwidth  = bitwidth )
                        )
      else:
        port_defs.append( 's.{port_name} = {port_type}( {bitwidth} )' \
                           .format( port_name = port_name,
                                    port_type = port_type,
                                    bitwidth  = bitwidth )
                        )

  # Assigning input ports
  set_inputs = []
  for v_name, bitwidth in in_ports:
    py_name = v_name.replace('_M_', '.')
    if '$' in py_name:
      name, idx = py_name.split('$')
      py_name = '{name}[{idx}]'.format( name = name, idx = int(idx) )
    v_name = verilator_mangle( v_name )
    set_inputs.append( 's._model.{v_name}[0] = s.{py_name}' \
                       .format( v_name = v_name, py_name = py_name )
                     )

  # Assigning combinational output ports
  set_outputs = []
  for v_name, bitwidth in out_ports:
    py_name = v_name.replace('_M_', '.')
    if '$' in py_name:
      name, idx = py_name.split('$')
      py_name = '{name}[{idx}]'.format( name = name, idx = int(idx) )
    v_name = verilator_mangle( v_name )
    set_outputs.append( 's.{py_name}.value = s._model.{v_name}[0]' \
                        .format( v_name = v_name, py_name = py_name )
                      )

  # TODO: no way to distinguish between combinational and sequential
  #       outputs, so we set outputs both ways...
  #       This seems broken, but I can't think of a better way.

  # Assigning sequential output ports
  set_next = []
  for v_name, bitwidth in out_ports:
    py_name = v_name.replace('_M_', '.')
    if '$' in py_name:
      name, idx = py_name.split('$')
      py_name = '{name}[{idx}]'.format( name = name, idx = int(idx) )
    v_name = verilator_mangle( v_name )
    set_next.append( 's.{py_name}.next = s._model.{v_name}[0]' \
                      .format( v_name = v_name, py_name = py_name )
                    )

  py_src = textwrap.dedent( py_src )
  py_src = py_src.format(
      model_name  = model_name,
      port_decls  = cdefs,
      port_defs   = '\n    '  .join( port_defs ),
      set_inputs  = '\n      '.join( set_inputs ),
      set_outputs = '\n      '.join( set_outputs ),
      set_next    = '\n      '.join( set_next ),
  )

  # TODO: need to set outports after tick eval()?
  #for i in out_ports:
  #  temp = i[0].replace('_M_', '.')
  #  if 'IDX' in i[0]:
  #    w += ('\n      s.{0}].next = s.{1}.{2}'
  #          .format( re.sub('IDX', '[', temp), xobj_name, i[0] ))
  #  else:
  #    w += ('\n      s.{0}.next = s.{1}.{2}'
  #          .format( temp, xobj_name, i[0] ))

  # TODO: needed for tracing?
  #w += 'XTraceEverOn()'

  f = open( filename_w, 'w' )
  f.write( py_src )
  f.close()


def verilator_mangle( signal_name ):
  return signal_name.replace( '__', '___05F' ).replace( '$', '__024' )
