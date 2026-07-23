import os
import pickle


REPO_DIR = os.path.dirname(__file__)


def _repo_path(filename):
    return os.path.join(REPO_DIR, filename)


def _load_pickle_file(filename, default_value):
    path = _repo_path(filename)
    try:
        with open(path, "rb") as file:
            return pickle.load(file)
    except FileNotFoundError:
        return default_value


def load_hb100_cal():
    """Load HB100 calibration signal frequency if it exists."""
    cal_file = _repo_path("hb100_cal.txt")
    if os.path.exists(cal_file):
        with open(cal_file, "r", encoding="utf-8") as f:
            try:
                return float(f.read().strip())
            except ValueError:
                pass
    raise FileNotFoundError("Calibration file not found or invalid.")


def save_hb100_cal(freq_hz):
    cal_file = _repo_path("hb100_cal.txt")
    with open(cal_file, "w", encoding="utf-8") as f:
        f.write(str(float(freq_hz)))
