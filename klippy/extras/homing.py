# Helper code for implementing homing operations
#
# Copyright (C) 2016-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, math

# NOTE:
#   -   https://github.com/Klipper3d/klipper/commit/78f4c25a14099564cf731bdaf5b97492a3a6fb47
#   -   https://github.com/Klipper3d/klipper/commit/dd34768e3afb6b5aa46885109182973d88df10b7
HOMING_START_DELAY = 0.001
ENDSTOP_SAMPLE_TIME = .000015
ENDSTOP_SAMPLE_COUNT = 4

# Return a completion that completes when all completions in a list complete
def multi_complete(printer, completions):
    if len(completions) == 1:
        return completions[0]
    # Build completion that waits for all completions
    reactor = printer.get_reactor()
    cp = reactor.register_callback(lambda e: [c.wait() for c in completions])
    # If any completion indicates an error, then exit main completion early
    for c in completions:
        reactor.register_callback(
            lambda e, c=c: cp.complete(1) if c.wait() else 0)
    return cp

# Tracking of stepper positions during a homing/probing move
class StepperPosition:
    def __init__(self, stepper, endstop_name):
        self.stepper = stepper
        self.endstop_name = endstop_name
        self.stepper_name = stepper.get_name()
        self.start_pos = stepper.get_mcu_position()
        self.halt_pos = self.trig_pos = None
        logging.info("\n\n" + f"StepperPosition: add stepper {self.stepper_name} to endstop {self.endstop_name}"+ "\n\n")
    def note_home_end(self, trigger_time):
        # NOTE: method called by "homing_move" to determine halt/trig positions.

        # NOTE: uses "itersolve_get_commanded_pos" to read "sk->commanded_pos" (at itersolve.c)
        self.halt_pos = self.stepper.get_mcu_position()
        # NOTE: uses "stepcompress_find_past_position" to:
        #       "Search history of moves to find a past position at a given clock"
        self.trig_pos = self.stepper.get_past_mcu_position(trigger_time)

# Implementation of homing/probing moves
class HomingMove:
    def __init__(self, printer, endstops, toolhead=None):
        self.printer = printer
        self.endstops = endstops
        # NOTE: The "HomingMove" class downstream methods use the
        #       following methods from a provided "toolhead" object:
        #       - flush_step_generation
        #       - get_kinematics:           returning a "kin" object with methods:
        #           - kin.get_steppers:     returning a list of stepper objects.
        #           - kin.calc_position:    returning ???
        #       - get_position:             returning "thpos" (toolhead position)
        #       - get_last_move_time:       returning "print_time" (and later "move_end_print_time")
        #       - dwell
        #       - drip_move
        #       - set_position
        if toolhead is None:
            toolhead = printer.lookup_object('toolhead')
        self.toolhead = toolhead
        self.stepper_positions = []
    
    def get_mcu_endstops(self):
        # NOTE: "self.endstops" is a list of tuples,
        #       containing elements like: (MCU_endstop, "name")
        #       This gets the MCU objects in a simple list.
        return [es for es, name in self.endstops]
    
    # NOTE: "_calc_endstop_rate" calculates the max amount of steps for the
    #       move, and the time the move will take. It then returns the "rate"
    #       of "time per step".
    def _calc_endstop_rate(self, mcu_endstop, movepos, speed):  # movepos  = [0.0, 0.0, 0.0, -110]
        startpos = self.toolhead.get_position()                 # startpos = [0.0, 0.0, 0.0, 0.0]
        axes_d = [mp - sp for mp, sp in zip(movepos, startpos)]
        move_d = math.sqrt(sum([d*d for d in axes_d[:3]]))      # 150.0
        move_t = move_d / speed                                 # 150.0 / 25.0 = 6.0
        max_steps = max([(abs(s.calc_position_from_coord(startpos)
                              - s.calc_position_from_coord(movepos))
                          / s.get_step_dist())
                         for s in mcu_endstop.get_steppers()])
        if max_steps <= 0.:
            return .001
        return move_t / max_steps
    
    def calc_toolhead_pos(self, kin_spos, offsets):
        # NOTE: the "kin_spos" received here has values from the
        #       "halting" position, before "oversteps" are corrected.
        #       For example:
        #           calc_toolhead_pos input: kin_spos={'extruder1': 0.0} offsets={'extruder1': -2273}
        # NOTE: "offsets" are probably in "step" units.
        kin_spos = dict(kin_spos)
        
        # NOTE: log input for reference
        logging.info(f"\n\ncalc_toolhead_pos input: kin_spos={str(kin_spos)} offsets={str(offsets)}\n\n")

        kin = self.toolhead.get_kinematics()
        for stepper in kin.get_steppers():
            sname = stepper.get_name()
            # NOTE: update the stepper positions by converting the "offset" steps
            #       to "mm" units and adding them to the original "halting" position.
            kin_spos[sname] += offsets.get(sname, 0) * stepper.get_step_dist()

        # NOTE: Repeat the above for the extruders.
        extruder_steppers = self.printer.lookup_extruder_steppers()  # [ExtruderStepper]
        for extruder_stepper in extruder_steppers:
            for stepper in extruder_stepper.rail.get_steppers():    # PrinterStepper (MCU_stepper)
                sname = stepper.get_name()
                kin_spos[sname] += offsets.get(sname, 0) * stepper.get_step_dist()
        
        # NOTE: this call to get_position is only used to acquire the extruder
        #       position, and append it to XYZ components below.
        #       Example:
        #           thpos=[0.0, 0.0, 0.0, 0.0]
        thpos = self.toolhead.get_position()

        # NOTE: The "calc_position" method iterates over the rails in the (cartesian)
        #       kinematics and selects "stepper_positions" with matching names.
        #       Perhaps other kinematics do something more elaborate.
        # NOTE: Elements 1-3 from the output are combined with element 4 from "thpos".
        #       This is likely because the 4th element is the extruder, which is not
        #       normally "homeable". So the last position is re-used to form the
        #       updated toolhead position vector.
        # NOTE: Examples (CartKinematics):
        #       -   calc_position input stepper_positions={'extruder': -1.420625}
        #       -   calc_position return pos=[-1.420625, 0.0, 0.0]
        result = list(kin.calc_position(stepper_positions=kin_spos))[:3] + thpos[3:]

        # NOTE: now ditch "thpos" (toolhead.get_position()), replacing 
        #       it by the equivalent for the active extruder.
        extruder = self.printer.lookup_object('toolhead').get_extruder()
        result[3] = kin_spos[extruder.name]
        
        # NOTE: log output for reference
        logging.info(f"\n\ncalc_toolhead_pos output: {str(result)}\n\n")
        # NOTE: example:
        #       calc_toolhead_pos output=[-1.420625, 0.0, 0.0, 0.0]

        # NOTE: this "result" is used to override "haltpos" below, which
        #       is then passed to "toolhead.set_position".
        return result
    
    def homing_move(self, movepos, speed, probe_pos=False,
                    triggered=True, check_triggered=True):
        # Notify start of homing/probing move
        self.printer.send_event("homing:homing_move_begin", self)
        
        # Note start location
        self.toolhead.flush_step_generation()

        # NOTE: the "get_kinematics" method is defined in the ToolHead 
        #       class at "toolhead.py". It apparently returns the kinematics
        #       object, as loaded from a module in the "kinematics/" directory,
        #       during the class's __init__.
        kin = self.toolhead.get_kinematics()
        
        # NOTE: this step calls the "get_steppers" method on the provided
        #       kinematics, which returns a list of "MCU_stepper" objects.
        kin_spos = {s.get_name(): s.get_commanded_position()
                    for s in kin.get_steppers()}

        # NOTE: Repeat the above for the extruders, adding them to the "kin_spos" dict.
        #       This is important later on, when calling "calc_toolhead_pos".
        extruder_steppers = self.printer.lookup_extruder_steppers()  # [ExtruderStepper]
        for extruder_stepper in extruder_steppers:
            # Get PrinterStepper (MCU_stepper) objects.
            for s in extruder_stepper.rail.get_steppers():
                kin_spos.update({s.get_name(): s.get_commanded_position()})
        
        # NOTE: "Tracking of stepper positions during a homing/probing move"
        #       Build a "StepperPosition" class for each of the steppers
        #       associated to each endstop in the "self.endstops" list of tuples,
        #       containing elements like: (MCU_endstop, "name").
        self.stepper_positions = [ StepperPosition(s, name)
                                   for es, name in self.endstops
                                   for s in es.get_steppers() ]
        # Start endstop checking
        print_time = self.toolhead.get_last_move_time()
        endstop_triggers = []
        for mcu_endstop, name in self.endstops:
            # NOTE: this calls "toolhead.get_position" to get "startpos".
            rest_time = self._calc_endstop_rate(mcu_endstop=mcu_endstop,
                                                movepos=movepos,  # [0.0, 0.0, 0.0, -110.0]
                                                speed=speed)
            # NOTE: "wait" is a "reactor.completion" object, returned by
            #       the "home_start" method of "MCU_endstop" (at mcu.py)
            wait = mcu_endstop.home_start(print_time=print_time, 
                                          sample_time=ENDSTOP_SAMPLE_TIME,
                                          sample_count=ENDSTOP_SAMPLE_COUNT, 
                                          rest_time=rest_time,
                                          triggered=triggered)
            endstop_triggers.append(wait)
        all_endstop_trigger = multi_complete(self.printer, endstop_triggers)

        # NOTE: This dwell used to be needed by low-power RPi2. Otherwise
        #       calculations would take too long, and by the time they were sent,
        #       the associated "mcu time" would have already passed.
        #       It was not needed after the implementation of drip moves.
        #       I don't know yet why it remains.
        self.toolhead.dwell(HOMING_START_DELAY)
        
        # Issue move
        error = None
        try:
            # NOTE: Before the "drip" commit, the following command 
            #       used to be: self.toolhead.move(movepos, speed)
            #       See: https://github.com/Klipper3d/klipper/commit/43064d197d6fd6bcc55217c5e9298d86bf4ecde7
            self.toolhead.drip_move(newpos=movepos,  # [0.0, 0.0, 0.0, -110.0]
                                    speed=speed, 
                                    # NOTE: "all_endstop_trigger" is probably made from
                                    #       the "reactor.completion" objects above.
                                    drip_completion=all_endstop_trigger)
        except self.printer.command_error as e:
            error = "Error during homing move: %s" % (str(e),)
        
        # Wait for endstops to trigger
        trigger_times = {}
        # NOTE: probably gets the time just after the last move.
        move_end_print_time = self.toolhead.get_last_move_time()
        for mcu_endstop, name in self.endstops:
            # NOTE: calls the "home_wait" method from "MCU_endstop".
            trigger_time = mcu_endstop.home_wait(move_end_print_time)
            if trigger_time > 0.:
                trigger_times[name] = trigger_time
            elif trigger_time < 0. and error is None:
                error = "Communication timeout during homing %s" % (name,)
            elif check_triggered and error is None:
                error = "No trigger on %s after full movement" % (name,)
        
        # Determine stepper halt positions
        # NOTE: "flush_step_generation" calls "flush" on the MoveQueue,
        #       and "_update_move_time" (which updates "self.print_time"
        #       and calls "trapq_finalize_moves").
        self.toolhead.flush_step_generation()
        
        for sp in self.stepper_positions:
            # NOTE: get the time of endstop triggering
            tt = trigger_times.get(sp.endstop_name, move_end_print_time)
            # NOTE: Record halt position from `stepper.get_mcu_position`,
            #       and trigger position from `stepper.get_past_mcu_position(tt)`
            #       in each StepperPosition class. This information is used below.
            sp.note_home_end(tt)
        
        # NOTE: calculate halting position from "oversteps" after triggering.
        #       This chunk was added in commit:
        #       https://github.com/Klipper3d/klipper/commit/3814a13251aeca044f6dbbccda706263040e1bec
        if probe_pos:
            halt_steps = {sp.stepper_name: sp.halt_pos - sp.start_pos
                          for sp in self.stepper_positions}
            trig_steps = {sp.stepper_name: sp.trig_pos - sp.start_pos
                          for sp in self.stepper_positions}
            haltpos = trigpos = self.calc_toolhead_pos(kin_spos=kin_spos, 
                                                       offsets=trig_steps)
            if trig_steps != halt_steps:
                haltpos = self.calc_toolhead_pos(kin_spos=kin_spos, 
                                                 offsets=halt_steps)
        else:
            haltpos = trigpos = movepos
            # NOTE: calculate "oversteps" after triggering, for each
            #       StepperPosition class.
            over_steps = {sp.stepper_name: sp.halt_pos - sp.trig_pos
                          for sp in self.stepper_positions}
            if any(over_steps.values()):
                # NOTE: "set_position" calls "flush_step_generation", and
                #       then uses "trapq_set_position" to write the position.
                #       It updates "commanded_pos" on the toolhead, and uses
                #       the "set_position" of the kinematics object (which 
                #       uses the set_position method of the rails/steppers).
                #       It ends by emittig a "toolhead:set_position" event.
                self.toolhead.set_position(movepos)
                # NOTE: from the "extruder_home" logs:
                #           set_position: input:  [0.0, 0.0, 0.0, -110.0] homing_axes=()
                #           set_position: output: [0.0, 0.0, 0.0, 0.0]  (i.e. passed to toolhead.set_position).
                
                # NOTE: uses "ffi_lib.itersolve_get_commanded_pos",
                #       probably reads the position previously set by
                #       "stepper.set_position" / "itersolve_set_position".
                halt_kin_spos = {s.get_name(): s.get_commanded_position()
                                 for s in kin.get_steppers()}

                # NOTE: Repeat the above for the extruder steppers (defined above).
                for extruder_stepper in extruder_steppers:
                    # Get PrinterStepper (MCU_stepper) objects.
                    for s in extruder_stepper.rail.get_steppers():
                        halt_kin_spos.update({s.get_name(): s.get_commanded_position()})
                
                # NOTE: calc_toolhead_pos input: kin_spos={'extruder1': 0.0} offsets={'extruder1': -2273}
                # NOTE: calc_toolhead_pos output: [-1.420625, 0.0, 0.0, 0.0]
                haltpos = self.calc_toolhead_pos(kin_spos=halt_kin_spos, 
                                                 offsets=over_steps)

        # NOTE: set the toolhead position to the (corrected) halting position.
        # NOTE: for extruder_home this could be:
        #           set_position: input=[-1.420625, 0.0, 0.0, 0.0] homing_axes=()
        #       The fourt element comes from "newpos_e" in the call to 
        #       "toolhead.set_position" above. The first element is the corrected
        #       "halt" position.
        self.toolhead.set_position(haltpos)
        
        # Signal homing/probing move complete
        try:
            # NOTE: event received by:
            #       - homing_heaters.py
            #       - probe.py
            #       - tmc.py
            #       Probably not relevant to extruder homing.
            self.printer.send_event("homing:homing_move_end", self)
        except self.printer.command_error as e:
            if error is None:
                error = str(e)
        
        # NOTE: raise any errors if found.
        #       This does not include the "trigger timeout" if
        #       if "check_triggered=False".
        if error is not None:
            raise self.printer.command_error(error)

        # NOTE: returns "trigpos", which is the position of the toolhead
        #       when the endstop triggered.
        return trigpos
    
    def check_no_movement(self, axes=None):
        """
        axes: list of the axes moving in a G38 probing move (x, y, z, extruder/extruder1). See "probe_axes".
        """
        if self.printer.get_start_args().get('debuginput') is not None:
            return None

        logging.info(f"\n\n" + "check_no_movement with axes: " + str(axes) + "\n\n")

        # NOTE: from the StepperPosition class:
        #       -   self.start_pos = stepper.get_mcu_position()
        #       -   self.trig_pos = self.stepper.get_past_mcu_position(trigger_time)
        for sp in self.stepper_positions:
            sp_name = sp.stepper_name

            if sp.start_pos == sp.trig_pos:
                # NOTE: Optionally return only if the stepper was involved in the probing.

                # NOTE: default behaviour, consider all steppers.
                if axes is None:
                    logging.info("\n\n" + f"check_no_movement matched stepper with default behaviour: {sp_name}" + "\n\n")
                    return sp.endstop_name

                # NOTE: This is the "G38" behaviour.
                # NOTE: Handle extruder steppers.
                elif sp_name.lower().startswith("extruder"):
                    if any(axis.lower() == sp_name.lower() for axis in axes):
                        # NOTE: If the stepper that did not move was the "active" one, return.
                        logging.info("\n\n" + f"check_no_movement matched stepper with G38 behaviour: {sp_name}" +"\n\n")
                        return sp.endstop_name
                # NOTE: Handle the XYZ axes. 
                elif any(axis.lower() in sp_name.lower() for axis in axes if axis in "xyz"): 
                    # NOTE: "Will return True if any of the substrings (axis)
                    #       in substring_list (axes) is contained in string (sp_name)."
                    #       See https://stackoverflow.com/a/8122096/11524079
                    logging.info("\n\n" + f"check_no_movement matched stepper with G38 behaviour: {sp_name}" +"\n\n")
                    return sp.endstop_name

        return None

# State tracking of homing requests
# NOTE: used here only by the cmd_G28 method from PrinterHoming. 
class Homing:
    def __init__(self, printer):
        self.printer = printer
        self.toolhead = printer.lookup_object('toolhead')
        self.changed_axes = []
        self.trigger_mcu_pos = {}
        self.adjust_pos = {}
    def set_axes(self, axes):
        self.changed_axes = axes
    def get_axes(self):
        return self.changed_axes
    def get_trigger_position(self, stepper_name):
        return self.trigger_mcu_pos[stepper_name]
    def set_stepper_adjustment(self, stepper_name, adjustment):
        self.adjust_pos[stepper_name] = adjustment
    def _fill_coord(self, coord):
        # Fill in any None entries in 'coord' with current toolhead position
        thcoord = list(self.toolhead.get_position())
        for i in range(len(coord)):
            if coord[i] is not None:
                thcoord[i] = coord[i]
        return thcoord
    def set_homed_position(self, pos):
        self.toolhead.set_position(self._fill_coord(pos))
    
    def home_rails(self, rails, forcepos, movepos):
        """_summary_

        Args:
            rails (list): A list of stepper "rail" objects.
            forcepos (list): A list of 4 coordinates, used to force the start position.
            movepos (list): A list of 4 coordinates, used to indicate the target (home) position.
        """
        # NOTE: this method is used by the home method of the 
        #       cartesian kinematics, in response to a G28 command.
        # NOTE: The "forcepos" argument is passed 1.5 times the
        #       difference between the endstop position and the
        #       opposing limit coordinate.
        # Notify of upcoming homing operation
        logging.info(f"\n\nhoming.home_rails: homing begins.\n\n")
        self.printer.send_event("homing:home_rails_begin", self, rails)
        # Alter kinematics class to think printer is at forcepos
        homing_axes = [axis for axis in range(3) if forcepos[axis] is not None]
        startpos = self._fill_coord(forcepos)
        homepos = self._fill_coord(movepos)
        # NOTE: esto usa "trapq_set_position" sobre el trapq del XYZ.
        # NOTE: homing_axes se usa finalmente en "CartKinematics.set_position",
        #       para asignarle limites a los "rails" que se homearon. Nada más.
        self.toolhead.set_position(startpos, homing_axes=homing_axes)
        # Perform first home
        endstops = [es for rail in rails for es in rail.get_endstops()]
        hi = rails[0].get_homing_info()
        hmove = HomingMove(self.printer, endstops)
        hmove.homing_move(homepos, hi.speed)
        # Perform second home
        if hi.retract_dist:
            # Retract
            startpos = self._fill_coord(forcepos)
            homepos = self._fill_coord(movepos)
            axes_d = [hp - sp for hp, sp in zip(homepos, startpos)]
            move_d = math.sqrt(sum([d*d for d in axes_d[:3]]))
            retract_r = min(1., hi.retract_dist / move_d)
            retractpos = [hp - ad * retract_r
                          for hp, ad in zip(homepos, axes_d)]
            self.toolhead.move(retractpos, hi.retract_speed)
            # Home again
            startpos = [rp - ad * retract_r
                        for rp, ad in zip(retractpos, axes_d)]
            self.toolhead.set_position(startpos)
            hmove = HomingMove(self.printer, endstops)
            hmove.homing_move(homepos, hi.second_homing_speed)
            if hmove.check_no_movement() is not None:
                raise self.printer.command_error(
                    "Endstop %s still triggered after retract"
                    % (hmove.check_no_movement(),))
        # Signal home operation complete
        self.toolhead.flush_step_generation()
        self.trigger_mcu_pos = {sp.stepper_name: sp.trig_pos
                                for sp in hmove.stepper_positions}
        self.adjust_pos = {}
        self.printer.send_event("homing:home_rails_end", self, rails)
        if any(self.adjust_pos.values()):
            # Apply any homing offsets
            kin = self.toolhead.get_kinematics()
            homepos = self.toolhead.get_position()
            kin_spos = {s.get_name(): (s.get_commanded_position()
                                       + self.adjust_pos.get(s.get_name(), 0.))
                        for s in kin.get_steppers()}
            newpos = kin.calc_position(kin_spos)
            for axis in homing_axes:
                homepos[axis] = newpos[axis]
            self.toolhead.set_position(homepos)

class PrinterHoming:
    def __init__(self, config):
        self.printer = config.get_printer()
        # Register g-code commands
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('G28', self.cmd_G28)
    def manual_home(self, toolhead, endstops, pos, speed,
                    triggered, check_triggered):
        hmove = HomingMove(self.printer, endstops, toolhead)
        try:
            hmove.homing_move(movepos=pos, speed=speed,  # probe_pos=False  # default value
                              triggered=triggered,
                              check_triggered=check_triggered)
        except self.printer.command_error:
            if self.printer.is_shutdown():
                raise self.printer.command_error(
                    "Homing failed due to printer shutdown")
            raise
    
    def probing_move(self, mcu_probe, pos, speed, check_triggered=True, 
                     # NOTE: Add a "triggered" argument. This is eventually used
                     #       to invert the probing logic at "_home_cmd.send()" 
                     #       in "mcu.py" to make the low-level "endstop_home" MCU command.
                     # NOTE: It is passed below to "homing_move". Reusing here the 
                     #       default "True" value from that method (to avoid issues
                     #       with other uses of probing_move).
                     triggered=True,
                     # NOTE: "probe_axes" should be a list of the axes
                     #       moving in this probing move.
                     probe_axes=None):
        """
        mcu_probe
        pos
        speed
        check_triggered
        triggered: logic invert for the endstop trigger.
        probe_axes: list of the axes moving in this probing move (x, y, z, extruder/extruder1).
        """
        
        endstops = [(mcu_probe, "probe")]
        hmove = HomingMove(self.printer, endstops)

        try:
            epos = hmove.homing_move(pos, speed, probe_pos=True, 
                                     # NOTE: Pass argument from "probing_move",
                                     #       to support G38.4/5 probing gcodes.
                                     triggered=triggered,
                                     # NOTE: Pass argument from "probing_move",
                                     #       to support G38.3/5 probing gcodes.
                                     check_triggered=check_triggered)
        except self.printer.command_error:
            if self.printer.is_shutdown():
                raise self.printer.command_error(
                    "Probing failed due to printer shutdown")
            raise
        
        # NOTE: this was getting raised for the G38 moves.
        #       "check_no_movement" looks at the stepper 
        #       start and trigger positions. If they are
        #       the same, then the error below is raised.
        #           "Probe triggered prior to movement"
        if hmove.check_no_movement(axes=probe_axes) is not None:
            raise self.printer.command_error(
                "Probe triggered prior to movement")
        
        return epos

    def cmd_G28(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        # Move to origin
        axes = []
        #for pos, axis in enumerate(toolhead.axis_names):
        for pos, axis in enumerate('XYZ'):
            if gcmd.get(axis, None) is not None:
                axes.append(pos)
        if not axes:
            # axes = list(range(toolhead.axis_count))
            axes = [0, 1, 2]
        homing_state = Homing(self.printer)
        homing_state.set_axes(axes)
        kin = toolhead.get_kinematics()
        try:
            # NOTE: In the cart kinematics, "kin.home" iterates over each 
            #       axis calling "Homing.home_rails", which then uses
            #       "HomingMove.homing_move" directly.
            kin.home(homing_state)
        except self.printer.command_error:
            if self.printer.is_shutdown():
                raise self.printer.command_error(
                    "Homing failed due to printer shutdown")
            self.printer.lookup_object('stepper_enable').motor_off()
            raise

def load_config(config):
    return PrinterHoming(config)
