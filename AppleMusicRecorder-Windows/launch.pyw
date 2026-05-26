import sys, os, traceback
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

try:
    import recorder
    recorder.main()
except Exception:
    log_path = os.path.join(os.getcwd(), "crash.log")
    with open(log_path, "w", encoding="utf-8") as f:
        traceback.print_exc(file=f)
