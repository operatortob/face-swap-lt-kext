#!/usr/bin/env python3

import os
import sys
import subprocess
import time

# single thread doubles performance of gpu-mode - needs to be set before torch import
if any(arg.startswith('--gpu-vendor') for arg in sys.argv):
    os.environ['OMP_NUM_THREADS'] = '1'
import platform
import signal
import shutil
import glob
import argparse
import psutil
import torch
import tensorflow
from pathlib import Path
import multiprocessing as mp
#from opennsfw2 import predict_video_frames, predict_image
import cv2

import roop.globals
from roop.swapper import process_video, process_img, process_faces, process_frames
from roop.utils import is_img, detect_fps, set_fps, create_video, add_audio, extract_frames, extract_frames_skip, rreplace
from roop.analyser import get_face_single
import roop.ui as ui

signal.signal(signal.SIGINT, lambda signal_number, frame: quit())
parser = argparse.ArgumentParser()
parser.add_argument('-f', '--face', help='use this face', dest='source_img')
parser.add_argument('-t', '--target', help='replace this face', dest='target_path')
parser.add_argument('-o', '--output', help='save output to this file', dest='output_file')
parser.add_argument('--keep-fps', help='maintain original fps', dest='keep_fps', action='store_true', default=False)
parser.add_argument('--keep-frames', help='keep frames directory', dest='keep_frames', action='store_true', default=False)
parser.add_argument('--all-faces', help='swap all faces in frame', dest='all_faces', action='store_true', default=False)
parser.add_argument('--max-memory', help='maximum amount of RAM in GB to be used', dest='max_memory', type=int)
parser.add_argument('--cpu-cores', help='number of CPU cores to use', dest='cpu_cores', type=int, default=max(psutil.cpu_count() / 2, 1))
parser.add_argument('--gpu-threads', help='number of threads to be use for the GPU', dest='gpu_threads', type=int, default=8)
parser.add_argument('--gpu-vendor', help='choice your GPU vendor', dest='gpu_vendor', choices=['apple', 'amd', 'intel', 'nvidia'])
parser.add_argument('--codeformer', help='use codeformer', dest='use_codeformer', action='store_true', default=False)
parser.add_argument('--codeformer-fidelity', help='Balance the quality (lower number) and fidelity (higher number)', dest='codeformer_fidelity', type=float, default=0.7)
parser.add_argument('--codeformer-realesrgan-upscale', help='Upscale', dest='codeformer_realesrgan_upscale', type=float, default=1)
parser.add_argument('--frame-skip', help='frame-skip', dest='frame_skip', type=int, default=0)
parser.add_argument('--times_to_interpolate', help='times_to_interpolate', dest='times_to_interpolate', type=int, default=2)


args = parser.parse_known_args()[0]

if 'all_faces' in args:
    roop.globals.all_faces = True

if args.use_codeformer:
    roop.globals.use_codeformer = True

if 'codeformer_fidelity' in args:
    roop.globals.codeformer_fidelity = args.codeformer_fidelity

if 'codeformer_realesrgan_upscale' in args:
    roop.globals.codeformer_realesrgan_upscale = args.codeformer_realesrgan_upscale

if args.frame_skip in args:
    roop.globals.frame_skip = int(args.frame_skip)

if args.times_to_interpolate in args:
    roop.globals.times_to_interpolate = int(args.times_to_interpolate)
    
if args.cpu_cores:
    roop.globals.cpu_cores = int(args.cpu_cores)

# cpu thread fix for mac
if sys.platform == 'darwin':
    roop.globals.cpu_cores = 1

if args.gpu_threads:
    roop.globals.gpu_threads = int(args.gpu_threads)

# gpu thread fix for amd
if args.gpu_vendor == 'amd':
    roop.globals.gpu_threads = 1

if args.gpu_vendor:
    roop.globals.gpu_vendor = args.gpu_vendor
else:
    roop.globals.providers = ['CPUExecutionProvider']

sep = "/"
if os.name == "nt":
    sep = "\\"


def limit_resources():
    # prevent tensorflow memory leak
    gpus = tensorflow.config.experimental.list_physical_devices('GPU')
    for gpu in gpus:
        tensorflow.config.experimental.set_memory_growth(gpu, True)
    if args.max_memory:
        memory = args.max_memory * 1024 * 1024 * 1024
        if str(platform.system()).lower() == 'windows':
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetProcessWorkingSetSize(-1, ctypes.c_size_t(memory), ctypes.c_size_t(memory))
        else:
            import resource
            resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))


def pre_check():
    if sys.version_info < (3, 9):
        quit('Python version is not supported - please upgrade to 3.9 or higher')
    if not shutil.which('ffmpeg'):
        quit('ffmpeg is not installed!')
    model_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), '../inswapper_128.onnx')
    if not os.path.isfile(model_path):
        quit('File "inswapper_128.onnx" does not exist!')
    if roop.globals.gpu_vendor == 'apple':
        if 'CoreMLExecutionProvider' not in roop.globals.providers:
            quit("You are using --gpu=apple flag but CoreML isn't available or properly installed on your system.")
    if roop.globals.gpu_vendor == 'amd':
        if 'ROCMExecutionProvider' not in roop.globals.providers:
            quit("You are using --gpu=amd flag but ROCM isn't available or properly installed on your system.")
    if roop.globals.gpu_vendor == 'nvidia':
        CUDA_VERSION = torch.version.cuda
        CUDNN_VERSION = torch.backends.cudnn.version()
        if not torch.cuda.is_available():
            quit("You are using --gpu=nvidia flag but CUDA isn't available or properly installed on your system.")
        if CUDA_VERSION > '11.8':
            quit(f"CUDA version {CUDA_VERSION} is not supported - please downgrade to 11.8")
        if CUDA_VERSION < '11.4':
            quit(f"CUDA version {CUDA_VERSION} is not supported - please upgrade to 11.8")
        if CUDNN_VERSION < 8220:
            quit(f"CUDNN version {CUDNN_VERSION} is not supported - please upgrade to 8.9.1")
        if CUDNN_VERSION > 8910:
            quit(f"CUDNN version {CUDNN_VERSION} is not supported - please downgrade to 8.9.1")


def get_video_frame(video_path, frame_number = 1):
    cap = cv2.VideoCapture(video_path)
    amount_of_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.set(cv2.CAP_PROP_POS_FRAMES, min(amount_of_frames, frame_number-1))
    if not cap.isOpened():
        print("Error opening video file")
        return
    ret, frame = cap.read()
    if ret:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    cap.release()


def preview_video(video_path):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print("Error opening video file")
        return 0
    amount_of_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    ret, frame = cap.read()
    if ret:
        frame = get_video_frame(video_path)

    cap.release()
    return (amount_of_frames, frame)


def status(string):
    value = "Status: " + string
    if 'cli_mode' in args:
        print(value)
    else:
        ui.update_status_label(value)


def process_video_multi_cores(source_img, frame_paths):
    n = len(frame_paths) // roop.globals.cpu_cores
    if n > 2:
        processes = []
        for i in range(0, len(frame_paths), n):
            p = POOL.apply_async(process_video, args=(source_img, frame_paths[i:i + n],))
            processes.append(p)
        for p in processes:
            p.get()
        POOL.close()
        POOL.join()


def start(preview_callback = None):
    if not args.source_img or not os.path.isfile(args.source_img):
        print("\n[WARNING] Please select an image containing a face.")
        return
    elif not args.target_path or not os.path.isfile(args.target_path):
        print("\n[WARNING] Please select a video/image to swap face in.")
        return
    if not args.output_file:
        target_path = args.target_path
        args.output_file = rreplace(target_path, "/", "/swapped-", 1) if "/" in target_path else "swapped-" + target_path
    target_path = args.target_path
    test_face = get_face_single(cv2.imread(args.source_img))
    if not test_face:
        print("\n[WARNING] No face detected in source image. Please try with another one.\n")
        return
    if is_img(target_path):
        # if predict_image(target_path) > 0.85:
        #     quit()
        process_img(args.source_img, target_path, args.output_file)
        if args.use_codeformer:
            status("inference codeformer...")
            codeformer_input_dir = os.path.dirname(target_path)
            subprocess.run(['python', '/content/CodeFormer/inference_codeformer.py','-w',f'{roop.globals.codeformer_fidelity}','--input_path',f'{codeformer_input_dir}','--output_path',f'{codeformer_input_dir}','--bg_upsampler','realesrgan','--face_upsample','-s',f'{roop.globals.codeformer_realesrgan_upscale}'], cwd='/content/CodeFormer')    
        status("swap successful!")
        return
    # seconds, probabilities = predict_video_frames(video_path=args.target_path, frame_interval=100)
    # if any(probability > 0.85 for probability in probabilities):
    #     quit()
    video_name_full = target_path.split("/")[-1]
    video_name = os.path.splitext(video_name_full)[0]
    output_dir = os.path.dirname(target_path) + "/" + video_name if os.path.dirname(target_path) else video_name
    Path(output_dir).mkdir(exist_ok=True)
    status("detecting video's FPS...")
    fps, exact_fps = detect_fps(target_path)
    if not args.keep_fps and fps > 30:
        this_path = output_dir + "/" + video_name + ".mp4"
        set_fps(target_path, this_path, 30)
        target_path, exact_fps = this_path, 30
    else:
        shutil.copy(target_path, output_dir)
    status("extracting frames...")
    if args.frame_skip:
        extract_frames_skip(target_path, output_dir, args.frame_skip)
    else:
        extract_frames(target_path, output_dir)
    args.frame_paths = tuple(sorted(
        glob.glob(output_dir + "/*.png"),
        key=lambda x: int(x.split(sep)[-1].replace(".png", ""))
    ))
    status("swapping in progress...")
    if roop.globals.gpu_vendor is None and roop.globals.cpu_cores > 1:
        global POOL
        POOL = mp.Pool(roop.globals.cpu_cores)
        process_video_multi_cores(args.source_img, args.frame_paths)
    else:
        process_video(args.source_img, args.frame_paths)
    
    if args.use_codeformer and not is_img(target_path):
        status("creating lowres video...")
        create_video(video_name, exact_fps, output_dir)
        status("inference codeformer...")
        subprocess.run(['python', '/content/CodeFormer/inference_codeformer.py','-w',f'{roop.globals.codeformer_fidelity}','--input_path',f'{output_dir}','--output_path',f'{output_dir}','--bg_upsampler','realesrgan','--face_upsample','-s',f'{roop.globals.codeformer_realesrgan_upscale}'], cwd='/content/CodeFormer')    
        
        
        
        final_results_output_dir = os.path.join(output_dir, "final_results")
        swapped_lowres_dir = os.path.join(output_dir, "swapped_lowres")
        # Создаем новую папку, если она не существует
        if not os.path.exists(swapped_lowres_dir):
            os.makedirs(swapped_lowres_dir)
        # Перемещение файлов с расширением .png в папку swapped_lowres
        for filename in os.listdir(output_dir):
            if filename.endswith(".png"):
                source_path = os.path.join(output_dir, filename)
                target_path = os.path.join(swapped_lowres_dir, filename)
                shutil.move(source_path, target_path)

        interpolated_frames_output_dir = os.path.join(final_results_output_dir, "interpolated_frames")
        if not os.path.exists(interpolated_frames_output_dir):
            os.makedirs(interpolated_frames_output_dir)

        status("frame interpolation...")
        subprocess.run(['python','-m','eval.interpolator_cli','--pattern',f'{final_results_output_dir}','--model_path','/content/frame-interpolation/pretrained_models/film_net/Style/saved_model','--times_to_interpolate',f'{args.times_to_interpolate}'], cwd='/content/frame-interpolation')    
        
       
        for filename in os.listdir(interpolated_frames_output_dir):
            if filename.endswith(".png"):
                source_path = os.path.join(interpolated_frames_output_dir, filename)
                target_path = os.path.join(output_dir, filename)
                shutil.copy2(source_path, target_path)
        
    status("creating video...")
    create_video(video_name, exact_fps, output_dir)
    status("adding audio...")
    add_audio(output_dir, target_path, video_name_full, args.keep_frames, args.output_file)
    save_path = args.output_file if args.output_file else output_dir + "/" + video_name + ".mp4"
    print("\n\nVideo saved as:", save_path, "\n\n")
    status("swap successful!")


def select_face_handler(path: str):
    args.source_img = path


def select_target_handler(path: str):
    args.target_path = path
    return preview_video(args.target_path)


def toggle_all_faces_handler(value: int):
    roop.globals.all_faces = True if value == 1 else False


def toggle_fps_limit_handler(value: int):
    args.keep_fps = int(value != 1)


def toggle_keep_frames_handler(value: int):
    args.keep_frames = value


def save_file_handler(path: str):
    args.output_file = path


def create_test_preview(frame_number):
    return process_faces(
        get_face_single(cv2.imread(args.source_img)),
        get_video_frame(args.target_path, frame_number)
    )


def run():
    global all_faces, keep_frames, limit_fps

    pre_check()
    limit_resources()
    if args.source_img:
        args.cli_mode = True
        start()
        quit()

    window = ui.init(
        {
            'all_faces': roop.globals.all_faces,
            'keep_fps': args.keep_fps,
            'keep_frames': args.keep_frames
        },
        select_face_handler,
        select_target_handler,
        toggle_all_faces_handler,
        toggle_fps_limit_handler,
        toggle_keep_frames_handler,
        save_file_handler,
        start,
        get_video_frame,
        create_test_preview
    )

    window.mainloop()
