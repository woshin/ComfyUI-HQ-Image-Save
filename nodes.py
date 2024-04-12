import os
import copy
import glob
from tqdm import tqdm, trange

os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"

import cv2 as cv
import torch
import numpy as np

import folder_paths
from comfy.cli_args import args
from comfy.utils import PROGRESS_BAR_ENABLED, ProgressBar


import OpenEXR
import Imath


def sRGBtoLinear(npArray):
    less = npArray <= 0.0404482362771082
    npArray[less] = npArray[less] / 12.92
    npArray[~less] = np.power((npArray[~less] + 0.055) / 1.055, 2.4)

def linearToSRGB(npArray):
    less = npArray <= 0.0031308
    npArray[less] = npArray[less] * 12.92
    npArray[~less] = np.power(npArray[~less], 1/2.4) * 1.055 - 0.055

def load_EXR(filepath, sRGB):
    exr_file = OpenEXR.InputFile(filepath)
    header = exr_file.header()
    dw = header['dataWindow']
    size = (dw.max.x - dw.min.x + 1, dw.max.y - dw.min.y + 1)
    pt = Imath.PixelType(Imath.PixelType.FLOAT)

    # 读取RGB通道
    r_str = exr_file.channel('ViewLayer.Combined.R', pt)
    g_str = exr_file.channel('ViewLayer.Combined.G', pt)
    b_str = exr_file.channel('ViewLayer.Combined.B', pt)
    a_str = exr_file.channel('ViewLayer.Combined.A', pt)

    d_str = exr_file.channel('ViewLayer.Depth.Z', pt)

    nx_str = exr_file.channel('ViewLayer.Normal.X', pt)
    ny_str = exr_file.channel('ViewLayer.Normal.Y', pt)
    nz_str = exr_file.channel('ViewLayer.Normal.Z', pt)

    i_str = exr_file.channel('ViewLayer.IndexOB.X', pt)

    # 将字符串转换为numpy数组
    r = np.fromstring(r_str, dtype=np.float32)
    g = np.fromstring(g_str, dtype=np.float32)
    b = np.fromstring(b_str, dtype=np.float32)
    a = np.fromstring(a_str, dtype=np.float32)

    d = np.fromstring(d_str, dtype=np.float32)
    nx = np.fromstring(nx_str, dtype=np.float32)
    ny = np.fromstring(ny_str, dtype=np.float32)
    nz = np.fromstring(nz_str, dtype=np.float32)

    i = np.fromstring(i_str, dtype=np.float32)

    # 重新整理数组至正确的图像维度
    r.shape = g.shape = b.shape = a.shape = d.shape = nx.shape = ny.shape = nz.shape = i.shape = (size[1], size[0])

    d -= np.min(d)
    d /= np.max(d[i > 0])
    d = 1.0 - d
    d[i < 1] = 0

    # 合并三个通道至一个三维数组
    rgb = np.stack((r, g, b), axis=-1)
    depth = np.stack((d, d, d), axis=-1)
    normal = np.stack((nx, ny, nz), axis=-1)

    # 归一化到0-1之间
    rgb_normalized = np.clip(rgb, 0, 1)
    depth_normalized = np.clip(depth, 0, 1)
    normal_normalized = (normal + 1.0) / 2.0 #np.clip(normal, 0, 1)

    if sRGB:
        linearToSRGB(rgb_normalized)
        rgb_normalized = np.clip(rgb_normalized, 0, 1)
    rgb = torch.unsqueeze(torch.from_numpy(rgb_normalized), 0)
    depth = torch.unsqueeze(torch.from_numpy(depth_normalized), 0)
    normal = torch.unsqueeze(torch.from_numpy(normal_normalized), 0)

    mask = torch.zeros((1, size[1], size[0]), dtype=torch.float32)
    mask[0] = torch.from_numpy(i)
    
    return (rgb, normal, depth, mask)

def load_EXR_latent(filepath):
    image = cv.imread(filepath, cv.IMREAD_UNCHANGED).astype(np.float32)
    image = image[:,:, np.array([2,1,0,3])]
    image = torch.unsqueeze(torch.from_numpy(image), 0)
    image = torch.movedim(image, -1, 1)
    return (image)

class LoadEXR:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "filepath": ("STRING", {"default": "path to directory or .exr file"}),
                "linear_to_sRGB": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "image_load_cap": ("INT", {"default": 0, "min": 0, "step": 1}),
                "skip_first_images": ("INT", {"default": 0, "min": 0, "step": 1}),
                "select_every_nth": ("INT", {"default": 1, "min": 1, "step": 1}),
            }
        }

    CATEGORY = "image"

    RETURN_TYPES = ("IMAGE", "IMAGE", "IMAGE", "MASK", "INT")
    RETURN_NAMES = ("RGB", "normal", "depth", "alpha", "batch_size")
    FUNCTION = "load"

    def load(self, filepath, linear_to_sRGB=True, image_load_cap=0, skip_first_images=0, select_every_nth=1):
        p = os.path.normpath(filepath.replace('\"', '').strip())
        if not os.path.exists(p):
            raise Exception("Path not found: " + p)
        
        if os.path.isfile(p) and os.path.splitext(p)[1].lower() == ".exr":
            rgb, normal, depth, mask = load_EXR(p, linear_to_sRGB)
            batch_size = 1
        else:
            rgb = []
            normal = []
            depth = []
            mask = []
            filelist = sorted(glob.glob(os.path.join(p, "*.exr")))
            if not filelist:
                filelist = sorted(glob.glob(os.path.join(p, "*.EXR")))
                if not filelist:
                    raise Exception("No EXRs found in folder")
            
            filelist = filelist[skip_first_images::select_every_nth]
            if image_load_cap > 0:
                cap = min(len(filelist), image_load_cap)
                filelist = filelist[:cap]
            batch_size = len(filelist)
            
            if PROGRESS_BAR_ENABLED:
                pbar = ProgressBar(batch_size)
            for file in tqdm(filelist, desc="loading images"):
                rgbFrame, normalFrame, depthFrame, maskFrame = load_EXR(file, linear_to_sRGB)
                rgb.append(rgbFrame)
                normal.append(normalFrame)
                depth.append(depthFrame)
                mask.append(maskFrame)
                if PROGRESS_BAR_ENABLED:
                    pbar.update(1)
            
            rgb = torch.cat(rgb, 0)
            normal = torch.cat(normal, 0)
            depth = torch.cat(depth, 0)
            mask = torch.cat(mask, 0)
        
        return (rgb, normal, depth, mask, batch_size)

class SaveEXR:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        # self.prefix_append = ""

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "images": ("IMAGE",),
                "filename_prefix": ("STRING", {"default": "ComfyUI"}),
                "sRGB_to_linear": ("BOOLEAN", {"default": True}),
                "version": ("INT", {"default": 1, "min": -1, "max": 999}),
                "start_frame": ("INT", {"default": 1001, "min": 0, "max": 99999999}),
                "frame_pad": ("INT", {"default": 4, "min": 1, "max": 8}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "save_images"

    OUTPUT_NODE = True

    CATEGORY = "image"

    def save_images(self, images, filename_prefix, sRGB_to_linear, version, start_frame, frame_pad, prompt=None, extra_pnginfo=None):
        useabs = os.path.isabs(filename_prefix)
        if not useabs:
            full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(filename_prefix, self.output_dir, images[0].shape[1], images[0].shape[0])
        results = list()
        
        linear = images.detach().clone().cpu().numpy().astype(np.float32)
        if sRGB_to_linear:
            sRGBtoLinear(linear[:,:,:,:3]) # only convert RGB, not Alpha
        
        bgr = copy.deepcopy(linear)
        bgr[:,:,:,0] = linear[:,:,:,2] # flip RGB to BGR for opencv
        bgr[:,:,:,2] = linear[:,:,:,0]
        if bgr.shape[-1] > 3:
            bgr[:,:,:,3] = np.clip(1 - linear[:,:,:,3], 0, 1) # invert alpha
        
        if version < 0:
            ver = ""
        else:
            ver = f"_v{version:03}"
        
        if useabs:
            basepath = filename_prefix
            if os.path.basename(filename_prefix) == "":
                basename = os.path.basename(os.path.normpath(filename_prefix))
                basepath = os.path.join(os.path.normpath(filename_prefix) + ver, basename)
            if not os.path.exists(os.path.dirname(basepath)):
                os.mkdir(os.path.dirname(basepath))
        
        batch_size = linear.shape[0]
        if PROGRESS_BAR_ENABLED and batch_size > 1:
            pbar = ProgressBar(batch_size)
        else:
            pbar = None
        for i in trange(batch_size, desc="saving images"):
            if useabs:
                writepath = basepath + ver + f".{str(start_frame + i).zfill(frame_pad)}.exr"
            else:
                file = f"{filename}_{counter:05}_.exr"
                writepath = os.path.join(full_output_folder, file)
                counter += 1
            
            if os.path.exists(writepath):
                raise Exception("File exists already, stopping to avoid overwriting")
                
            header = OpenEXR.Header(bgr[i].shape[0], bgr[i].shape[1])
            half_chan = Imath.Channel(Imath.PixelType(Imath.PixelType.FLOAT))
            header['channels'] = dict([(c, half_chan) for c in "RGB"])

            red_str = bgr[i][:,:,2].tobytes()
            green_str = bgr[i][:,:,1].tobytes()
            blue_str = bgr[i][:,:,0].tobytes()

            exr_file = OpenEXR.OutputFile(writepath, header)
            exr_file.writePixels({'R': red_str, 'G': green_str, 'B': blue_str})
            exr_file.close()

            if pbar is not None:
                pbar.update(1)

        return { "ui": { "images": results } }

class SaveTiff:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        self.prefix_append = ""

    @classmethod
    def INPUT_TYPES(s):
        return {"required": 
                    {"images": ("IMAGE", ),
                     "filename_prefix": ("STRING", {"default": "ComfyUI"})},
                "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
                }

    RETURN_TYPES = ()
    FUNCTION = "save_images"

    OUTPUT_NODE = True

    CATEGORY = "image"

    def save_images(self, images, filename_prefix="ComfyUI", prompt=None, extra_pnginfo=None):
        import imageio
        
        filename_prefix += self.prefix_append
        full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(filename_prefix, self.output_dir, images[0].shape[1], images[0].shape[0])
        results = list()
        for image in images:
            i = 65535. * image.cpu().numpy()
            img = np.clip(i, 0, 65535).astype(np.uint16)
            file = f"{filename}_{counter:05}_.tiff"
            imageio.imwrite(os.path.join(full_output_folder, file), img)
            #results.append({
            #    "filename": file,
            #    "subfolder": subfolder,
            #    "type": self.type
            #})
            counter += 1

        return { "ui": { "images": results } }

class LoadLatentEXR:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "filepath": ("STRING", {"default": "path to directory or .exr file"}),
            },
            "optional": {
                "image_load_cap": ("INT", {"default": 0, "min": 0, "step": 1}),
                "skip_first_images": ("INT", {"default": 0, "min": 0, "step": 1}),
                "select_every_nth": ("INT", {"default": 1, "min": 1, "step": 1}),
            }
        }

    CATEGORY = "latent"

    RETURN_TYPES = ("LATENT", "INT")
    RETURN_NAMES = ("samples", "batch_size")
    FUNCTION = "load"

    def load(self, filepath, image_load_cap=0, skip_first_images=0, select_every_nth=1):
        p = os.path.normpath(filepath.replace('\"', '').strip())
        if not os.path.exists(p):
            raise Exception("Path not found: " + p)
        
        if os.path.isfile(p) and os.path.splitext(p)[1].lower() == ".exr":
            samples = load_EXR_latent(p)
            batch_size = 1
        else:
            samples = []
            filelist = sorted(glob.glob(os.path.join(p, "*.exr")))
            if not filelist:
                filelist = sorted(glob.glob(os.path.join(p, "*.EXR")))
                if not filelist:
                    raise Exception("No EXRs found in folder")
            
            filelist = filelist[skip_first_images::select_every_nth]
            if image_load_cap > 0:
                cap = min(len(filelist), image_load_cap)
                filelist = filelist[:cap]
            batch_size = len(filelist)
            
            if PROGRESS_BAR_ENABLED:
                pbar = ProgressBar(batch_size)
            for file in tqdm(filelist, desc="loading latents"):
                sampleFrame = load_EXR_latent(file)
                samples.append(sampleFrame)
                if PROGRESS_BAR_ENABLED:
                    pbar.update(1)
            
            samples = torch.cat(samples, 0)
        
        return ({"samples": samples}, batch_size)

class SaveLatentEXR:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        # self.prefix_append = ""

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "samples": ("LATENT",),
                "filename_prefix": ("STRING", {"default": "ComfyUI"}),
                "version": ("INT", {"default": 1, "min": -1, "max": 999}),
                "start_frame": ("INT", {"default": 1001, "min": 0, "max": 99999999}),
                "frame_pad": ("INT", {"default": 4, "min": 1, "max": 8}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "save_images"

    OUTPUT_NODE = True

    CATEGORY = "latent"

    def save_images(self, samples, filename_prefix, version, start_frame, frame_pad, prompt=None, extra_pnginfo=None):
        useabs = os.path.isabs(filename_prefix)
        linear = torch.movedim(samples["samples"], 1, -1)
        linear = linear.detach().clone().cpu().numpy().astype(np.float32)
        
        if not useabs:
            full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(filename_prefix, self.output_dir, linear[0].shape[1], linear[0].shape[0])
        results = list()
        
        # flip rgb -> bgr for opencv
        linear = linear[:,:,:, np.array([2,1,0,3])]
        
        if version < 0:
            ver = ""
        else:
            ver = f"_v{version:03}"
        
        if useabs:
            basepath = filename_prefix
            if os.path.basename(filename_prefix) == "":
                basename = os.path.basename(os.path.normpath(filename_prefix))
                basepath = os.path.join(os.path.normpath(filename_prefix) + ver, basename)
            if not os.path.exists(os.path.dirname(basepath)):
                os.mkdir(os.path.dirname(basepath))
        
        batch_size = linear.shape[0]
        if PROGRESS_BAR_ENABLED and batch_size > 1:
            pbar = ProgressBar(batch_size)
        else:
            pbar = None
        for i in trange(batch_size, desc="saving latents"):
            if useabs:
                writepath = basepath + ver + f".{str(start_frame + i).zfill(frame_pad)}.exr"
            else:
                file = f"{filename}_{counter:05}_.exr"
                writepath = os.path.join(full_output_folder, file)
                counter += 1
            
            if os.path.exists(writepath):
                raise Exception("File exists already, stopping to avoid overwriting")
            cv.imwrite(writepath, linear[i])
            if pbar is not None:
                pbar.update(1)

        return { "ui": { "images": results } }


NODE_CLASS_MAPPINGS = {
    "LoadEXR": LoadEXR,
    "SaveEXR": SaveEXR,
    "SaveTiff": SaveTiff,
    "LoadLatentEXR": LoadLatentEXR,
    "SaveLatentEXR": SaveLatentEXR,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LoadEXR": "Load EXR",
    "SaveEXR": "Save EXR",
    "SaveTiff": "Save Tiff",
    "LoadLatentEXR": "Load Latent EXR",
    "SaveLatentEXR": "Save Latent EXR",
}