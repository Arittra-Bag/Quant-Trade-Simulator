import time

# Initialize collection for performance metrics
latency_measurements = []

def measure_latency(start_time):
    """
    Measure latency given a start time and store in history.
    
    This function calculates the time elapsed since the start_time in milliseconds.
    It also adds the measurement to a running history to enable statistical analysis.
    
    Args:
        start_time (float): Start time in seconds (from time.time())
    
    Returns:
        float: Elapsed time in milliseconds
    """
    latency = (time.time() - start_time) * 1000  # ms
    latency_measurements.append(latency)
    
    # Keep only the last 1000 measurements to prevent unlimited growth
    if len(latency_measurements) > 1000:
        latency_measurements.pop(0)
    
    return latency 