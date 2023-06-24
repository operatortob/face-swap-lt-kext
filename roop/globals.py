import onnxruntime

all_faces = None
log_level = 'error'
cpu_cores = None
gpu_threads = None
gpu_vendor = None
providers = onnxruntime.get_available_providers()
use_codeformer = None
codeformer_fidelity = None
codeformer_realesrgan_upscale = None
frame_skip = None
times_to_interpolate = None

if 'TensorrtExecutionProvider' in providers:
    providers.remove('TensorrtExecutionProvider')
