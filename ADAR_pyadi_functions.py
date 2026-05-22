import time

def ADAR_init(device):
    """ Initialize ADAR1000 """
    device.reset()
    time.sleep(0.1)

def ADAR_set_mode(device, mode):
    """ Set rx/tx mode """
    device.mode = mode

def ADAR_set_Taper(array, taper_list):
    """ Set array taper gains """
    # Map elements 1..8 to their gains
    for i in range(8):
        element_id = i + 1
        array.elements[element_id].rx_gain = int(taper_list[i])

def ADAR_set_Phase(array, PhDelta, phase_step_size, phaseList):
    """ Set array phases for given steering angle/delta """
    for i in range(8):
        element_id = i + 1
        base_phase = phaseList[i] + i * PhDelta
        # Quantize it
        q_phase = round(base_phase / phase_step_size) * phase_step_size
        
        # Keep between 0 and 360
        q_phase = q_phase % 360
        if q_phase < 0:
            q_phase += 360
            
        array.elements[element_id].rx_phase = q_phase
