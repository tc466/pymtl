#=======================================================================
# SimulationTool.py
#=======================================================================
# Tool for simulating hardware models.
#
# This module contains classes which construct a model simulator for
# execution in the Python interpreter.

from __future__ import print_function

import pprint
import collections
import inspect
import warnings
import sim_utils as sim

from sys               import flags
from SimulationMetrics import SimulationMetrics, DummyMetrics

#-----------------------------------------------------------------------
# SimulationTool
#-----------------------------------------------------------------------
# User visible class implementing a tool for simulating hardware models.
#
# This class takes a model instance and creates a simulator for
# execution in the Python interpreter.
class SimulationTool( object ):

  #---------------------------------------------------------------------
  # __init__
  #---------------------------------------------------------------------
  # Construct a simulator based on the provided model.
  def __init__( self, model, collect_metrics = False ):

    # Check that the model has been elaborated
    if not model.is_elaborated():
      raise Exception( "cannot initialize {0} tool.\n"
                       "Provided model has not been elaborated yet!!!"
                       "".format( self.__class__.__name__ ) )

    self.model                = model
    self.ncycles              = 0

    self._event_queue         = EventQueue()
    self._sequential_blocks   = []
    self._register_queue      = []
    self._current_func        = None

    self._nets                = None # TODO: remove me

    #self._DEBUG_signal_cbs    = collections.defaultdict(list)


    # Collect statistics if configured and model supports stats
    self.collect_stats = bool(getattr(model, 'stats_file', None)) and \
        hasattr(model, 'reg_stats') and hasattr(model, 'tick_stats')

    if self.collect_stats:
      model.reg_stats()

    # Only collect metrics if they are enabled, otherwise replace
    # with a dummy collection class.

    if collect_metrics:
      self.metrics            = SimulationMetrics()
    else:
      self.metrics            = DummyMetrics()

    # If the -O flag was passed to Python, use the perf implementation
    # of cycle, otherwise use the dev version.

    if flags.optimize:
      self.cycle              = self._perf_cycle
      self.eval_combinational = self._perf_eval
    else:
      self.cycle              = self._dev_cycle
      self.eval_combinational = self._dev_eval


    # Construct a simulator for the provided model.

    signals                 = sim.collect_signals( model )
    nets, slice_connections = sim.signals_to_nets( signals )
    sequential_blocks       = sim.register_seq_blocks( model )

    sim.insert_signal_values( self, nets )

    sim.register_comb_blocks  ( model, self._event_queue )
    sim.create_slice_callbacks( slice_connections, self._event_queue )
    sim.register_cffi_updates ( model )

    self._nets              = nets
    self._sequential_blocks = sequential_blocks

    # Setup vcd dumping if it's configured

    if hasattr( model, 'vcd_file' ) and model.vcd_file:
      from vcd import VCDUtil
      VCDUtil( self, model.vcd_file )

  #---------------------------------------------------------------------
  # reset
  #---------------------------------------------------------------------
  # Sets the reset signal high and cycles the simulator.
  def reset( self ):
    self.model.reset.v = 1
    self.cycle()
    self.cycle()
    self.model.reset.v = 0

  #---------------------------------------------------------------------
  # print_line_trace
  #---------------------------------------------------------------------
  # Print cycle number and line trace of model.
  def print_line_trace( self ):
    print( "{:>3}:".format( self.ncycles ), self.model.line_trace() )

  #---------------------------------------------------------------------
  # cycle
  #---------------------------------------------------------------------
  # Advances the simulator by a single clock cycle, executing all
  # sequential @tick and @posedge_clk blocks defined in the design, as
  # well as any @combinational blocks that have been added to the event
  # queue.
  #
  # Note: see _debug_cycle and _perf_cycle for actual implementations.
  def cycle( self ):
    pass

  #---------------------------------------------------------------------
  # _debug_cycle
  #---------------------------------------------------------------------
  # Implementation of cycle() for use during develop-test-debug loops.
  def _dev_cycle( self ):

    # Call all events generated by input changes
    self.eval_combinational()

    # Clock generation needed by VCD tracing
    self.model.clk.value = 0
    self.model.clk.value = 1

    # Distinguish between events caused by input vectors changing (above)
    # and events caused by clocked logic (below).
    self.metrics.start_tick()

    # Tick stats
    if self.collect_stats:
      self.model.tick_stats()

    # Call all rising edge triggered functions
    for func in self._sequential_blocks:
      func()

    # Then flop the shadow state on all registers
    while self._register_queue:
      reg = self._register_queue.pop()
      reg.flop()

    # Call all events generated by synchronous logic
    self.eval_combinational()

    # Increment the simulator cycle count
    self.ncycles += 1

    # Tell the metrics module to prepare for the next cycle
    self.metrics.incr_metrics_cycle()

  #---------------------------------------------------------------------
  # _perf_cycle
  #---------------------------------------------------------------------
  # Implementation of cycle() for use when benchmarking models.
  def _perf_cycle( self ):

    # Call all events generated by input changes
    self.eval_combinational()

    # Call all rising edge triggered functions
    for func in self._sequential_blocks:
      func()

    # Then flop the shadow state on all registers
    while self._register_queue:
      reg = self._register_queue.pop()
      reg.flop()

    # Call all events generated by synchronous logic
    self.eval_combinational()

    # Increment the simulator cycle count
    self.ncycles += 1

  #---------------------------------------------------------------------
  # eval_combinational
  #---------------------------------------------------------------------
  # Evaluate all combinational logic blocks currently in the eventqueue.
  def eval_combinational( self ):
    pass

  #---------------------------------------------------------------------
  # _debug_eval
  #---------------------------------------------------------------------
  # Implementation of eval_combinational() for use during
  # develop-test-debug loops.
  def _dev_eval( self ):
    while self._event_queue.len():
      self._current_func = func = self._event_queue.deq()
      self.metrics.incr_comb_evals( func )
      func()
      self._current_func = None

  #---------------------------------------------------------------------
  # _perf_eval
  #---------------------------------------------------------------------
  # Implementation of eval_combinataional () for use when benchmarking
  # models.
  def _perf_eval( self ):
    while self._event_queue.len():
      self._current_func = func = self._event_queue.deq()
      func()
      self._current_func = None

  #---------------------------------------------------------------------
  # add_event
  #---------------------------------------------------------------------
  # Add an event to the simulator event queue for later execution.
  #
  # This function will check if the written SignalValue instance has any
  # registered events (functions decorated with @combinational), and if
  # so, adds them to the event queue.
  def add_event( self, signal_value ):
    # TODO: debug_event
    #print("    ADDEVENT: VALUE", signal_value.v,  end='')
    #print(signal_value in self._DEBUG_signal_cbs, end='')
    #print([x.fullname for x in signal_value._DEBUG_signal_names], end='')
    #print(self._DEBUG_signal_cbs[signal_value])

    self.metrics.incr_add_events()

    # Place all other callbacks in the event queue for execution later

    for func in signal_value._callbacks:
      self.metrics.incr_add_callbk()
      if func != self._current_func:
        self._event_queue.enq( func.cb, func.id )

#-----------------------------------------------------------------------
# EventQueue
#-----------------------------------------------------------------------
class EventQueue( object ):

  def __init__( self, initsize = 1000 ):
    self.fifo     = collections.deque()
    self.func_bv  = [ False ] * initsize
    self.func_ids = 0

  def enq( self, event, id ):
    if not self.func_bv[ id ]:
      self.func_bv[ id ] = True
      self.fifo.appendleft( event )

  def deq( self ):
    event = self.fifo.pop()
    self.func_bv[ event.id ] = False
    return event

  def len( self ):
    return len( self.fifo )

  def __len__( self ):
    return len( self.fifo )

  def get_id( self ):
    id = self.func_ids
    self.func_ids += 1
    if self.func_ids > len( self.func_bv ):
      self.func_bv.extend( [ False ] * 1000 )
    return id
