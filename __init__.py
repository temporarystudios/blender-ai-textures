bl_info = {
    "name": "AI Texture Generator",
    "description": "Generate textures using AI models (Stable Diffusion XL and Flux Pro) directly within Blender, powered by the Replicate API.",
    "author": "Temporary Studio",
    "blender": (1, 0, 0),
    "category": "Material",
    "module": "ai_texture_generator"
}

import bpy
import os
import requests
import time
import uuid
from bpy.props import StringProperty, EnumProperty, BoolProperty, FloatProperty, IntProperty
from bpy.types import Operator, Panel, AddonPreferences, PropertyGroup
from threading import Thread, current_thread as threading_current_thread
import threading
from queue import Queue
from enum import Enum

def update_ui_status(context, status):
    context.scene.progress_status = status
    if threading.current_thread() is threading.main_thread():
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                area.tag_redraw()

def debug_status(context):
    print("\nDebug Status:")
    print(f"Active Object: {context.active_object.name if context.active_object else 'None'}")
    print(f"Has Materials: {bool(context.active_object and hasattr(context.active_object.data, 'materials'))}")
    print(f"Current Status: {context.scene.progress_status}")
    print(f"Current Prompt: {context.scene.ai_texture_generator_text_prompt}")

def download_image(image_url, download_path="/tmp", context=None):
    try:
        if context:
            update_ui_status(context, "Downloading Image...")
        response = requests.get(image_url)
        response.raise_for_status()
        
        os.makedirs(download_path, exist_ok=True)
        
        image_name = os.path.basename(image_url)
        image_path = os.path.join(download_path, image_name)
        
        with open(image_path, 'wb') as image_file:
            image_file.write(response.content)
        
        if context:
            update_ui_status(context, "Image Downloaded")
        return image_path
    except requests.RequestException as e:
        print(f"Error downloading image: {e}")
        if context:
            update_ui_status(context, "Download Failed")
        return None

def create_normal_map(texture_node, nodes, links, location_offset):
    """Create a normal map setup from the base texture"""

    normal_map = nodes.new('ShaderNodeNormalMap')
    normal_map.location = (location_offset[0] + 200, location_offset[1] - 300)
    
    bump = nodes.new('ShaderNodeBump')
    bump.location = (location_offset[0] + 400, location_offset[1] - 300)
    
    rgb_to_bw = nodes.new('ShaderNodeRGBToBW')
    rgb_to_bw.location = (location_offset[0], location_offset[1] - 300)
    
    links.new(texture_node.outputs['Color'], rgb_to_bw.inputs['Color'])
    links.new(rgb_to_bw.outputs['Val'], normal_map.inputs['Strength'])
    links.new(normal_map.outputs['Normal'], bump.inputs['Normal'])
    
    return bump

def create_roughness_map(texture_node, nodes, links, location_offset):
    """Create a roughness setup from the base texture"""

    rgb_to_bw = nodes.new('ShaderNodeRGBToBW')
    rgb_to_bw.location = (location_offset[0], location_offset[1] - 600)
    
    color_ramp = nodes.new('ShaderNodeValToRGB')
    color_ramp.location = (location_offset[0] + 200, location_offset[1] - 600)
    
    color_ramp.color_ramp.elements[0].position = 0.2
    color_ramp.color_ramp.elements[1].position = 0.8
    
    links.new(texture_node.outputs['Color'], rgb_to_bw.inputs['Color'])
    links.new(rgb_to_bw.outputs['Val'], color_ramp.inputs[0])
    
    return color_ramp

def load_image_as_texture(image_path, text_prompt, image_uuid, context):

    addon_prefs = context.preferences.addons["ai_texture_generator"].preferences
    model_name = addon_prefs.active_model.lower()
    
    unique_name = f"{model_name}_{text_prompt[:20]}_{image_uuid}"
    
    image = bpy.data.images.load(image_path, check_existing=False)
    image.name = unique_name
    
    material_name = f"AI_Material_{model_name}_{sanitize_name(text_prompt)}_{image_uuid}"
    
    context.scene.progress_status = "Updating Texture Node..."
    
    obj = context.active_object
    if not obj:
        print("No active object found")
        context.scene.progress_status = "Error: No active object"
        return False
    
    if not hasattr(obj.data, "materials"):
        print(f"Object type {obj.type} cannot have materials")
        context.scene.progress_status = "Error: Object cannot have materials"
        return False
        
    counter = 1
    while material_name in bpy.data.materials:
        material_name = f"AI_Material_{model_name}_{sanitize_name(text_prompt)}_{counter}_{image_uuid}"
        counter += 1
    
    material = bpy.data.materials.new(name=material_name)
    material.use_nodes = True
    
    obj.data.materials.append(material)
    new_slot_index = len(obj.data.materials) - 1
    obj.active_material_index = new_slot_index
    
    if obj.mode == 'EDIT':
        bpy.ops.object.mode_set(mode='OBJECT')
    
    if hasattr(obj.data, "polygons"):
        for face in obj.data.polygons:
            face.material_index = new_slot_index
    elif hasattr(obj.data, "materials"):
        obj.material_slots[new_slot_index].link = 'OBJECT'
        obj.active_material = material
    
    try:
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        
        nodes.clear()
        
        output = nodes.new('ShaderNodeOutputMaterial')
        output.location = (600, 300)
        
        principled = nodes.new('ShaderNodeBsdfPrincipled')
        principled.location = (300, 300)
        
        mapping = nodes.new('ShaderNodeMapping')
        mapping.location = (-300, 300)
        
        texcoord = nodes.new('ShaderNodeTexCoord')
        texcoord.location = (-500, 300)
        
        texture = nodes.new('ShaderNodeTexImage')
        texture.location = (-100, 300)
        texture.name = f"AI_Texture_Node_{image_uuid}"
        texture.image = image
        
        links.new(texcoord.outputs['UV'], mapping.inputs['Vector'])
        links.new(mapping.outputs['Vector'], texture.inputs['Vector'])
        links.new(texture.outputs['Color'], principled.inputs['Base Color'])
        links.new(principled.outputs['BSDF'], output.inputs['Surface'])
        
        if context.scene.ai_texture_props.use_normal_map:
            bump = create_normal_map(texture, nodes, links, texture.location)
            links.new(bump.outputs['Normal'], principled.inputs['Normal'])
        
        if context.scene.ai_texture_props.use_roughness:
            roughness = create_roughness_map(texture, nodes, links, texture.location)
            links.new(roughness.outputs['Color'], principled.inputs['Roughness'])
        
        mapping.inputs['Scale'].default_value[0] = context.scene.ai_texture_props.tiling_x
        mapping.inputs['Scale'].default_value[1] = context.scene.ai_texture_props.tiling_y
        
        context.scene.progress_status = "Texture Node Updated"
        print(f"Created and applied new material: {material.name}")
        return True
        
    except Exception as e:
        print(f"Error while setting up nodes: {str(e)}")
        context.scene.progress_status = f"Error: {str(e)}"
        return False

def sanitize_name(name):
    """Convert prompt text to a valid material name"""

    valid_name = "".join(c for c in name if c.isalnum() or c in (' ', '_', '-'))
    valid_name = valid_name.strip().replace(' ', '_')
    return valid_name[:32]

def create_preview_thumbnail(image, size=128):
    """Create a thumbnail version of the image"""
    if not image:
        return None
        
    thumb_name = f"thumb_{image.name}"
    
    if thumb_name in bpy.data.images:
        return bpy.data.images[thumb_name]
    
    def create_thumb():
        thumb = image.copy()
        thumb.name = thumb_name
        thumb.scale(size, size)
        return thumb
    
    bpy.app.timers.register(create_thumb, first_interval=0.1)
    return image

class AIModelType(Enum):
    SDXL = "7762fd07cf82c948538e41f63f77d685e02b063e37e496e96eefd46c929f9bdc"
    FLUX = "2a65f3e9-6ef7-4ba1-9673-78e4d01ac20c"

class AIModelSettings(PropertyGroup):
    width: IntProperty(
        name="Width",
        description="Width of output image",
        default=1024,
        min=256,
        max=2048
    )
    height: IntProperty(
        name="Height",
        description="Height of output image",
        default=1024,
        min=256,
        max=2048
    )
    
    scheduler: EnumProperty(
        name="Scheduler",
        description="Scheduler type (SDXL only)",
        items=[
            ('DDIM', "DDIM", "DDIM scheduler"),
            ('DPMSolverMultistep', "DPM-Solver++", "DPM-Solver++ scheduler"),
            ('HeunDiscrete', "Heun", "Heun scheduler"),
            ('KarrasDPM', "DPM", "Karras DPM scheduler"),
            ('K_EULER_ANCESTRAL', "Euler-A", "Euler Ancestral scheduler"),
            ('K_EULER', "Euler", "Euler scheduler"),
            ('PNDM', "PNDM", "PNDM scheduler")
        ],
        default='K_EULER'
    )
    
    refine: EnumProperty(
        name="Refine Style",
        description="Which refine style to use",
        items=[
            ('no_refiner', "No Refiner", "No refinement"),
            ('expert_ensemble_refiner', "Expert Ensemble", "Expert ensemble refiner"),
            ('base_image_refiner', "Base Image", "Base image refiner")
        ],
        default='no_refiner'
    )
    
    guidance_scale: FloatProperty(
        name="Guidance Scale",
        description="Scale for classifier-free guidance",
        default=7.5,
        min=1.0,
        max=50.0
    )
    
    num_inference_steps: IntProperty(
        name="Steps",
        description="Number of denoising steps",
        default=50,
        min=1,
        max=500
    )
    
    prompt_strength: FloatProperty(
        name="Prompt Strength",
        description="Strength of the prompt",
        default=0.8,
        min=0.0,
        max=1.0
    )
    
    apply_watermark: BoolProperty(
        name="Apply Watermark",
        description="Apply watermark to generated images",
        default=True
    )
    
    aspect_ratio: EnumProperty(
        name="Aspect Ratio",
        description="Aspect ratio for the generated image (Flux only)",
        items=[
            ('custom', "Custom", "Use custom width and height"),
            ('1:1', "1:1", "Square"),
            ('16:9', "16:9", "Widescreen"),
            ('3:2', "3:2", "Standard"),
            ('2:3', "2:3", "Portrait"),
            ('4:5', "4:5", "Portrait"),
            ('5:4', "5:4", "Landscape"),
            ('9:16', "9:16", "Mobile"),
            ('3:4', "3:4", "Portrait"),
            ('4:3', "4:3", "Landscape")
        ],
        default='1:1'
    )
    
    guidance: FloatProperty(
        name="Guidance",
        description="Controls prompt adherence vs. image quality (Flux only)",
        default=3.0,
        min=2.0,
        max=5.0
    )
    
    interval: FloatProperty(
        name="Interval",
        description="Controls output variance (Flux only)",
        default=2.0,
        min=1.0,
        max=4.0
    )
    
    safety_tolerance: IntProperty(
        name="Safety Tolerance",
        description="Safety filter strength, 1 is strict, 6 is permissive (Flux only)",
        default=2,
        min=1,
        max=6
    )
    
    prompt_upsampling: BoolProperty(
        name="Prompt Upsampling",
        description="Automatically enhance the prompt (Flux only)",
        default=False
    )
    
    output_format: EnumProperty(
        name="Output Format",
        description="Format for saved images (Flux only)",
        items=[
            ('webp', "WebP", "WebP format"),
            ('jpg', "JPEG", "JPEG format"),
            ('png', "PNG", "PNG format")
        ],
        default='webp'
    )
    
    output_quality: IntProperty(
        name="Output Quality",
        description="Image quality for JPG/WebP (Flux only)",
        default=80,
        min=0,
        max=100
    )

class AITextureGeneratorPreferences(AddonPreferences):
    bl_idname = "ai_texture_generator"
    
    api_key: StringProperty(
        name="API Key",
        description="API Key for the Replicate API",
        default="",
        subtype='PASSWORD'
    )
    
    save_location: EnumProperty(
        name="Save Location",
        description="Where to save the generated images",
        items=[
            ('BLENDER', "Blender File", "Save in the Blender file"),
            ('FOLDER', "Next to Blender File", "Save in the same folder as the Blender file")
        ],
        default='FOLDER'
    )
    
    active_model: EnumProperty(
        name="AI Model",
        description="Select the AI model to use",
        items=[
            ('SDXL', "Stable Diffusion XL", "High-quality image generation with SDXL"),
            ('FLUX', "Flux Pro", "Advanced image generation with Flux Pro"),
        ],
        default='SDXL'
    )

    def draw(self, context):
        layout = self.layout
        
        box = layout.box()
        box.label(text="General Settings:")
        box.prop(self, "api_key")
        box.prop(self, "save_location")
        box.prop(self, "active_model")
        
        box = layout.box()
        box.label(text="Model Settings:")
        model_settings = context.scene.ai_model_settings
        
        if self.active_model == 'SDXL':
            col = box.column(align=True)
            col.prop(model_settings, "scheduler")
            col.prop(model_settings, "refine")
            col.prop(model_settings, "guidance_scale")
            col.prop(model_settings, "num_inference_steps")
            col.prop(model_settings, "prompt_strength")
            col.prop(model_settings, "apply_watermark")
        else:
            col = box.column(align=True)
            col.prop(model_settings, "aspect_ratio")
            if model_settings.aspect_ratio == 'custom':
                col.prop(model_settings, "width")
                col.prop(model_settings, "height")
            col.prop(model_settings, "guidance")
            col.prop(model_settings, "interval")
            col.prop(model_settings, "num_inference_steps", text="Steps")
            col.prop(model_settings, "safety_tolerance")
            col.prop(model_settings, "prompt_upsampling")
            col.prop(model_settings, "output_format")
            if model_settings.output_format != 'png':
                col.prop(model_settings, "output_quality")

class StatusQueue:
    def __init__(self):
        self._queue = Queue()
        self._lock = threading.Lock()
    
    def put(self, status):
        with self._lock:
            self._queue.put(status)
    
    def get(self):
        with self._lock:
            return self._queue.get() if not self._queue.empty() else None

class AITextureGenerator(Operator):
    bl_idname = "material.ai_texture_generator"
    bl_label = "Generate Texture"
    
    _timer = None
    _thread = None
    _queue = Queue()
    _status_queue = StatusQueue()
    _prediction_id = None
    
    def modal(self, context, event):
        if event.type == 'TIMER':
            status = self._status_queue.get()
            if status:
                context.scene.progress_status = status
                for area in context.screen.areas:
                    if area.type == 'PROPERTIES':
                        area.tag_redraw()
            
            if not self._prediction_id:
                if not self._queue.empty():
                    self._prediction_id = self._queue.get()
                    if self._prediction_id is None:
                        self.cancel(context)
                        return {'CANCELLED'}
                    print(f"Got prediction ID: {self._prediction_id}")
            else:
                try:
                    addon_prefs = context.preferences.addons["ai_texture_generator"].preferences
                    api_key = addon_prefs.api_key
                    headers = {"Authorization": f"Bearer {api_key}"}
                    
                    poll_url = f"https://api.replicate.com/v1/predictions/{self._prediction_id}"
                    print(f"Polling: {poll_url}")
                    
                    poll_response = requests.get(poll_url, headers=headers)
                    response_data = poll_response.json()
                    print(f"Poll response: {response_data}")
                    
                    status = response_data['status']
                    if status == 'processing':
                        logs = response_data.get('logs', '')
                        if logs:
                            progress_lines = [line for line in logs.split('\n') if '%|' in line]
                            if progress_lines:
                                last_progress = progress_lines[-1]
                                try:
                                    percent = last_progress.split('%')[0].strip()
                                    update_ui_status(context, f"Generating: {percent}%")
                                except:
                                    update_ui_status(context, "Generating...")
                        else:
                            update_ui_status(context, "Generating...")
                    else:
                        update_ui_status(context, f"Status: {status.title()}")
                    
                    if status == 'succeeded':
                        addon_prefs = context.preferences.addons["ai_texture_generator"].preferences
                        if addon_prefs.active_model == 'SDXL':
                            image_url = response_data['output'][0]
                        else:
                            image_url = response_data['output']
                            
                        print(f"Got output URL: {image_url}")
                        
                        image_path = download_image(image_url)
                        print(f"Downloaded to: {image_path}")
                        
                        if not image_path or not os.path.exists(image_path):
                            self.report({'ERROR'}, "Failed to download generated image")
                            self.cancel(context)
                            return {'CANCELLED'}
                        
                        file_size = os.path.getsize(image_path)
                        if file_size == 0:
                            print(f"Error: Downloaded file is empty: {image_path}")
                            self.report({'ERROR'}, "Downloaded file is empty")
                            self.cancel(context)
                            return {'CANCELLED'}
                        
                        print(f"Downloaded file size: {file_size} bytes")
                        
                        image_uuid = uuid.uuid4()
                        
                        if addon_prefs.save_location == 'FOLDER':
                            blend_file_directory = os.path.dirname(bpy.data.filepath)
                            target_path = os.path.join(blend_file_directory, 
                                f"{image_uuid}_{os.path.basename(image_path)}")
                            os.rename(image_path, target_path)
                            self.report({'INFO'}, f"Image saved to {target_path}")
                        else:
                            target_path = image_path
                            self.report({'INFO'}, "Image saved in blend file")
                        
                        try:
                            if load_image_as_texture(target_path, 
                                context.scene.ai_texture_generator_text_prompt, 
                                image_uuid,
                                context):
                                self.report({'INFO'}, "Texture applied successfully")
                            else:
                                self.report({'WARNING'}, "Image saved but couldn't apply texture")
                        except Exception as e:
                            self.report({'ERROR'}, f"Error applying texture: {str(e)}")
                            print(f"Error details: {str(e)}")
                        
                        if addon_prefs.save_location == 'BLENDER' and os.path.exists(target_path):
                            try:
                                os.remove(target_path)
                            except Exception as e:
                                print(f"Warning: Could not remove temporary file: {e}")
                        
                        self.cancel(context)
                        return {'FINISHED'}
                        
                    elif status == 'failed':
                        error_msg = response_data.get('error', 'Unknown error')
                        print(f"Generation failed: {error_msg}")
                        self.report({'ERROR'}, f"Generation failed: {error_msg}")
                        self.cancel(context)
                        return {'CANCELLED'}
                        
                except Exception as e:
                    print(f"Error during generation: {str(e)}")
                    self.report({'ERROR'}, f"Error during generation: {str(e)}")
                    self.cancel(context)
                    return {'CANCELLED'}
        
        return {'PASS_THROUGH'}
    
    def execute(self, context):
        print("Starting texture generation...")
        debug_status(context)
        
        if not context.active_object:
            self.report({'ERROR'}, "No active object selected")
            return {'CANCELLED'}
        
        if not hasattr(context.active_object.data, "materials"):
            self.report({'ERROR'}, f"Object type {context.active_object.type} cannot have materials")
            return {'CANCELLED'}
        
        addon_prefs = None
        for addon_name in bpy.context.preferences.addons.keys():
            if addon_name.endswith("ai_texture_generator"):
                addon_prefs = bpy.context.preferences.addons[addon_name].preferences
                break
        
        if not addon_prefs:
            self.report({'ERROR'}, "Could not find addon preferences")
            return {'CANCELLED'}
        
        if not addon_prefs.api_key:
            self.report({'ERROR'}, "Please enter your API key in the addon preferences")
            return {'CANCELLED'}
        
        prompt = context.scene.ai_texture_generator_text_prompt.strip()
        if not prompt:
            self.report({'ERROR'}, "Please enter a text prompt")
            return {'CANCELLED'}
        
        if addon_prefs.save_location == 'FOLDER' and not bpy.data.filepath:
            self.report({'ERROR'}, "Please save your blend file first")
            return {'CANCELLED'}
        
        context.scene.progress_status = "Submitting prediction..."
        
        def submit_prediction():
            try:
                self._status_queue.put("Preparing submission...")
                print("Starting generation submission...")
                
                addon_prefs = context.preferences.addons["ai_texture_generator"].preferences
                api_key = addon_prefs.api_key
                headers = {
                    "Authorization": f"Bearer {api_key}", 
                    "Content-Type": "application/json",
                    "Prefer": "wait"
                }
                model_settings = context.scene.ai_model_settings
                
                if addon_prefs.active_model == 'SDXL':
                    url = "https://api.replicate.com/v1/predictions"
                    data = {
                        "version": AIModelType[addon_prefs.active_model].value,
                        "input": {
                            "prompt": context.scene.ai_texture_generator_text_prompt,
                            "width": model_settings.width,
                            "height": model_settings.height,
                            "refine": model_settings.refine,
                            "num_inference_steps": int(model_settings.num_inference_steps),
                            "apply_watermark": bool(model_settings.apply_watermark)
                        }
                    }
                else:
                    url = f"https://api.replicate.com/v1/models/black-forest-labs/flux-pro/predictions"
                    data = {
                        "input": {
                            "prompt": context.scene.ai_texture_generator_text_prompt,
                            "width": model_settings.width,
                            "height": model_settings.height
                        }
                    }
                
                print(f"Submitting prediction with data: {data}")
                response = requests.post(
                    url,
                    json=data,
                    headers=headers
                )
                
                if response.status_code == 201:
                    prediction_id = response.json()['id']
                    self._status_queue.put("Submission accepted, starting generation...")
                    print(f"Prediction submitted, ID: {prediction_id}")
                    self._queue.put(prediction_id)
                else:
                    self._status_queue.put("Submission failed")
                    print(f"Prediction submission failed: {response.text}")
                    self._queue.put(None)
                    
            except Exception as e:
                print(f"Error in submit_prediction: {str(e)}")
                self._queue.put(None)
        
        self._thread = Thread(target=submit_prediction)
        self._thread.start()
        
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.5, window=context.window)
        wm.modal_handler_add(self)
        
        return {'RUNNING_MODAL'}
    
    def cancel(self, context):
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

class AITextureGeneratorPanel(Panel):
    bl_label = "AI Texture Generator"
    bl_idname = "MATERIAL_PT_ai_texture_generator"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "material"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        
        box = layout.box()
        box.label(text="Generate New Texture", icon='ADD')
        
        status = context.scene.progress_status
        if status != "Waiting...":
            status_box = box.box()
            status_box.label(text=status, icon='INFO')
        
        prompt_box = box.box()
        prompt_box.label(text="Text Prompt:")
        col = prompt_box.column()
        col.prop(context.scene, "ai_texture_generator_text_prompt", text="")
        
        size_row = box.row(align=True)
        size_row.prop(context.scene.ai_model_settings, "width", text="Width")
        size_row.prop(context.scene.ai_model_settings, "height", text="Height")
        
        box.operator("material.ai_texture_generator")
        
        if not obj or not obj.material_slots:
            return
            
        active_mat = obj.active_material
        if active_mat and active_mat.name.startswith("AI_Material_"):
            box = layout.box()
            box.label(text="Active Texture Settings", icon='MATERIAL')
            
            row = box.row()
            if '_' in active_mat.name:
                display_name = active_mat.name.split('_')[2]
            else:
                display_name = active_mat.name
            row.label(text=display_name)
            row.operator("material.ai_texture_delete", text="", icon='X').material_name = active_mat.name
            
            split = box.split(factor=0.3)
            
            preview_col = split.column()
            texture_node = next((n for n in active_mat.node_tree.nodes 
                if n.type == 'TEX_IMAGE'), None)
            if texture_node and texture_node.image:
                preview_col.template_ID_preview(
                    texture_node, 
                    "image",
                    hide_buttons=True,
                    rows=3,
                    cols=1
                )
            
            settings_col = split.column()
            settings_col.prop(context.scene.ai_texture_props, "tiling_x")
            settings_col.prop(context.scene.ai_texture_props, "tiling_y")
            settings_col.prop(context.scene.ai_texture_props, "use_normal_map")
            settings_col.prop(context.scene.ai_texture_props, "use_roughness")
            settings_col.operator("material.ai_texture_update", text="Apply Settings")
            settings_col.separator()
            settings_col.label(text="Upscale Settings:")
            settings_col.prop(context.scene.ai_texture_props, "upscale_factor")
            settings_col.prop(context.scene.ai_texture_props, "face_enhance")
            settings_col.operator("material.ai_texture_upscale", text="Upscale Texture")
        
        box = layout.box()
        row = box.row()
        row.label(text="Generated Textures", icon='MATERIAL_DATA')
        
        grid_flow = box.grid_flow(row_major=True, columns=4, even_columns=True, even_rows=True)
        
        for slot in obj.material_slots:
            if slot.material and slot.material.name.startswith("AI_Material_"):
                mat = slot.material
                cell = grid_flow.box()
                
                row = cell.row(align=True)
                
                texture_node = next((n for n in mat.node_tree.nodes 
                    if n.type == 'TEX_IMAGE'), None)
                if texture_node and texture_node.image:
                    row.template_icon(
                        icon_value=texture_node.image.preview.icon_id,
                        scale=2
                    )
                
                is_active = (mat == active_mat)
                name_col = row.column()
                if '_' in mat.name:
                    parts = mat.name.split('_')
                    if len(parts) > 3:
                        model_name = parts[2].upper()
                        if "upscaled" in mat.name.lower():
                            texture_node = next((n for n in mat.node_tree.nodes 
                                if n.type == 'TEX_IMAGE'), None)
                            if texture_node and texture_node.image:
                                img_parts = texture_node.image.name.split('_')
                                upscale_info = next((p for p in img_parts if 'x_' in p), '')
                                if upscale_info:
                                    model_name = f"{model_name} ↑{upscale_info.split('_')[0]}"
                                else:
                                    model_name = f"{model_name} ↑"
                        display_name = f"{model_name}: {parts[3][:10]}"
                    else:
                        display_name = mat.name[:10]
                else:
                    display_name = mat.name[:10]
                name_col.label(text=display_name)
                
                button_row = cell.row(align=True)
                button_row.scale_y = 0.8
                
                select_op = button_row.operator("material.ai_texture_select", 
                    text="Select" if not is_active else "Active",
                    depress=is_active)
                select_op.material_name = mat.name
                
                assign_op = button_row.operator("material.ai_texture_assign", 
                    text="Assign", icon='CHECKMARK')
                assign_op.material_name = mat.name
                
                button_row.operator("material.ai_texture_delete", 
                    text="", icon='X').material_name = mat.name

class AITextureProperties(PropertyGroup):
    tiling_x: FloatProperty(
        name="Tiling X",
        description="Horizontal tiling",
        default=1.0,
        min=0.1,
    )
    tiling_y: FloatProperty(
        name="Tiling Y",
        description="Vertical tiling",
        default=1.0,
        min=0.1,
    )
    use_normal_map: BoolProperty(
        name="Generate Normal Map",
        description="Generate a normal map from the texture",
        default=False,
    )
    use_roughness: BoolProperty(
        name="Generate Roughness",
        description="Generate a roughness map from the texture",
        default=False,
    )
    upscale_factor: FloatProperty(
        name="Upscale Factor",
        description="Factor to scale image by",
        default=2.0,
        min=1.0,
        max=10.0,
    )
    face_enhance: BoolProperty(
        name="Face Enhance",
        description="Run GFPGAN face enhancement along with upscaling",
        default=False,
    )

class AITextureDelete(Operator):
    bl_idname = "material.ai_texture_delete"
    bl_label = "Delete AI Texture"
    bl_options = {'REGISTER', 'UNDO'}
    
    material_name: StringProperty()
    
    def execute(self, context):
        mat = bpy.data.materials.get(self.material_name)
        if mat:
            bpy.data.materials.remove(mat)
        return {'FINISHED'}

class AITextureUpdate(Operator):
    bl_idname = "material.ai_texture_update"
    bl_label = "Update Texture Settings"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        obj = context.active_object
        if not obj or not obj.active_material:
            return {'CANCELLED'}
            
        material = obj.active_material
        if not material.use_nodes:
            return {'CANCELLED'}
            
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        
        texture_node = next((n for n in nodes if n.type == 'TEX_IMAGE'), None)
        principled = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
        
        if not texture_node or not principled:
            return {'CANCELLED'}
            
        mapping_node = next((n for n in nodes if n.type == 'MAPPING'), None)
        texcoord_node = next((n for n in nodes if n.type == 'TEX_COORD'), None)
        
        if not mapping_node:
            mapping_node = nodes.new('ShaderNodeMapping')
            mapping_node.location = (texture_node.location.x - 200, texture_node.location.y)
        
        if not texcoord_node:
            texcoord_node = nodes.new('ShaderNodeTexCoord')
            texcoord_node.location = (mapping_node.location.x - 200, mapping_node.location.y)
        
        links.new(texcoord_node.outputs['UV'], mapping_node.inputs['Vector'])
        links.new(mapping_node.outputs['Vector'], texture_node.inputs['Vector'])
        links.new(texture_node.outputs['Color'], principled.inputs['Base Color'])
        
        mapping_node.inputs['Scale'].default_value[0] = context.scene.ai_texture_props.tiling_x
        mapping_node.inputs['Scale'].default_value[1] = context.scene.ai_texture_props.tiling_y
        
        normal_nodes = [n for n in nodes if n.type in {'NORMAL_MAP', 'BUMP'}]
        if context.scene.ai_texture_props.use_normal_map:
            if not normal_nodes:
                bump = create_normal_map(texture_node, nodes, links, texture_node.location)
                links.new(bump.outputs['Normal'], principled.inputs['Normal'])
        else:
            for node in normal_nodes:
                nodes.remove(node)
            rgb_to_bw = next((n for n in nodes if n.type == 'RGBTOBW' 
                and n.location[1] < texture_node.location[1]), None)
            if rgb_to_bw:
                nodes.remove(rgb_to_bw)
        
        roughness_nodes = [n for n in nodes if n.type == 'VALTORGB' 
            and n.location[1] < texture_node.location[1]]
        if context.scene.ai_texture_props.use_roughness:
            if not roughness_nodes:
                roughness = create_roughness_map(texture_node, nodes, links, texture_node.location)
                links.new(roughness.outputs['Color'], principled.inputs['Roughness'])
        else:
            for node in roughness_nodes:
                nodes.remove(node)
            rgb_to_bw = next((n for n in nodes if n.type == 'RGBTOBW' 
                and n.location[1] < texture_node.location[1] - 300), None)
            if rgb_to_bw:
                nodes.remove(rgb_to_bw)
        
        return {'FINISHED'}

class AITextureSelect(Operator):
    bl_idname = "material.ai_texture_select"
    bl_label = "Select Texture"
    bl_options = {'REGISTER', 'UNDO'}
    
    material_name: StringProperty()
    
    def execute(self, context):
        obj = context.active_object
        if not obj:
            return {'CANCELLED'}
            
        for i, slot in enumerate(obj.material_slots):
            if slot.material and slot.material.name == self.material_name:
                obj.active_material_index = i
                break
                
        return {'FINISHED'}

class AITextureAssign(Operator):
    bl_idname = "material.ai_texture_assign"
    bl_label = "Assign Texture"
    bl_options = {'REGISTER', 'UNDO'}
    
    material_name: StringProperty()
    
    def execute(self, context):
        obj = context.active_object
        if not obj:
            return {'CANCELLED'}
            
        mat = bpy.data.materials.get(self.material_name)
        if not mat:
            return {'CANCELLED'}
            
        if obj.mode == 'EDIT' and hasattr(obj.data, "polygons"):
            mat_idx = -1
            for i, slot in enumerate(obj.material_slots):
                if slot.material == mat:
                    mat_idx = i
                    break
            
            if mat_idx >= 0:
                original_active_index = obj.active_material_index
                
                obj.active_material_index = mat_idx
                
                bpy.context.tool_settings.mesh_select_mode = (False, False, True)
                bpy.ops.object.material_slot_assign()
                
                obj.active_material_index = original_active_index
        else:
            found = False
            for i, slot in enumerate(obj.material_slots):
                if slot.material == mat:
                    found = True
                    break
            
            if not found:
                obj.data.materials.append(mat)
            
        return {'FINISHED'}

class AITextureUpscale(Operator):
    bl_idname = "material.ai_texture_upscale"
    bl_label = "Upscale Texture"
    bl_options = {'REGISTER', 'UNDO'}
    
    _timer = None
    _thread = None
    _queue = Queue()
    _status_queue = StatusQueue()
    _prediction_id = None
    
    def modal(self, context, event):
        if event.type == 'TIMER':
            status = self._status_queue.get()
            if status:
                context.scene.progress_status = status
                for area in context.screen.areas:
                    if area.type == 'PROPERTIES':
                        area.tag_redraw()
            
            if not self._prediction_id:
                if not self._queue.empty():
                    self._prediction_id = self._queue.get()
                    if self._prediction_id is None:
                        self.cancel(context)
                        return {'CANCELLED'}
                    print(f"Got prediction ID: {self._prediction_id}")
            else:
                try:
                    addon_prefs = context.preferences.addons["ai_texture_generator"].preferences
                    api_key = addon_prefs.api_key
                    headers = {"Authorization": f"Bearer {api_key}"}
                    
                    poll_url = f"https://api.replicate.com/v1/predictions/{self._prediction_id}"
                    print(f"Polling: {poll_url}")
                    
                    poll_response = requests.get(poll_url, headers=headers)
                    response_data = poll_response.json()
                    print(f"Poll response: {response_data}")
                    
                    status = response_data['status']
                    update_ui_status(context, f"Upscaling Status: {status}")
                    
                    if status == 'succeeded':
                        image_url = response_data['output']
                        print(f"Got output URL: {image_url}")
                        
                        addon_prefs = context.preferences.addons["ai_texture_generator"].preferences
                        if addon_prefs.save_location == 'FOLDER':
                            if not bpy.data.filepath:
                                self.report({'ERROR'}, "Please save your blend file first")
                                self.cancel(context)
                                return {'CANCELLED'}
                            save_dir = os.path.dirname(bpy.data.filepath)
                        else:
                            save_dir = "/tmp"
                        
                        image_uuid = uuid.uuid4()
                        filename = f"upscaled_{self._prediction_id}_{image_uuid}.png"
                        image_path = os.path.join(save_dir, filename)
                        
                        image_path = download_image(image_url, download_path=save_dir, context=context)
                        if image_path:
                            new_path = os.path.join(os.path.dirname(image_path), filename)
                            os.rename(image_path, new_path)
                            image_path = new_path
                        
                        print(f"Downloaded to: {image_path}")
                        
                        material = context.active_object.active_material
                        texture_node = next((n for n in material.node_tree.nodes 
                            if n.type == 'TEX_IMAGE'), None)
                        
                        if texture_node and texture_node.image:
                            try:
                                print(f"Loading image from: {image_path}")
                                new_image = bpy.data.images.load(image_path, check_existing=False)
                                
                                upscale_factor = int(context.scene.ai_texture_props.upscale_factor)
                                new_image.name = f"upscaled_{upscale_factor}x_{texture_node.image.name}"
                                print(f"Loaded new image: {new_image.name}")
                                
                                new_image.reload()
                                
                                if new_image.size[0] > 0 and new_image.size[1] > 0 and new_image.channels > 0:
                                    print(f"Image verified: {new_image.size[0]}x{new_image.size[1]} ({new_image.channels} channels)")
                                    
                                    if not new_image.packed_file:
                                        try:
                                            print("Packing image...")
                                            new_image.pack()
                                            print("Image packed successfully")
                                        except Exception as e:
                                            print(f"Error packing image: {str(e)}")
                                            self.report({'ERROR'}, "Failed to pack image")
                                            self.cancel(context)
                                            return {'CANCELLED'}
                                    
                                    old_image = texture_node.image
                                    texture_node.image = new_image
                                    
                                    material = context.active_object.active_material
                                    material.update_tag()
                                    material.node_tree.update_tag()
                                    new_image.update_tag()
                                    
                                    for area in context.screen.areas:
                                        if area.type in ['VIEW_3D', 'IMAGE_EDITOR', 'NODE_EDITOR']:
                                            area.tag_redraw()
                                    
                                    if os.path.exists(image_path):
                                        try:
                                            os.remove(image_path)
                                        except Exception as e:
                                            print(f"Warning: Could not remove temporary file: {e}")
                                    
                                    self.report({'INFO'}, "Texture upscaled successfully")
                                    print("Texture upscale complete")
                                    
                                else:
                                    print(f"Error: Invalid image properties")
                                    print(f"Size: {new_image.size[0]}x{new_image.size[1]}")
                                    print(f"Channels: {new_image.channels}")
                                    self.report({'ERROR'}, "Invalid image properties")
                                    self.cancel(context)
                                    return {'CANCELLED'}
                                    
                            except Exception as e:
                                print(f"Error loading/applying image: {str(e)}")
                                self.report({'ERROR'}, f"Error applying image: {str(e)}")
                                self.cancel(context)
                                return {'CANCELLED'}
                        else:
                            self.report({'ERROR'}, "Could not find texture node")
                        
                        self.cancel(context)
                        return {'FINISHED'}
                        
                    elif status == 'failed':
                        error_msg = response_data.get('error', 'Unknown error')
                        print(f"Upscaling failed: {error_msg}")
                        self.report({'ERROR'}, f"Upscaling failed: {error_msg}")
                        self.cancel(context)
                        return {'CANCELLED'}
                        
                except Exception as e:
                    print(f"Error during upscaling: {str(e)}")
                    self.report({'ERROR'}, f"Error during upscaling: {str(e)}")
                    self.cancel(context)
                    return {'CANCELLED'}
        
        return {'PASS_THROUGH'}
    
    def execute(self, context):
        print("Starting upscale operation...")
        
        material = context.active_object.active_material
        if not material or not material.use_nodes:
            self.report({'ERROR'}, "No active material with nodes")
            return {'CANCELLED'}
        
        texture_node = next((n for n in material.node_tree.nodes 
            if n.type == 'TEX_IMAGE'), None)
        
        if not texture_node or not texture_node.image:
            self.report({'ERROR'}, "No texture found in material")
            return {'CANCELLED'}
        
        addon_prefs = context.preferences.addons["ai_texture_generator"].preferences
        if not addon_prefs or not addon_prefs.api_key:
            self.report({'ERROR'}, "Please enter your API key in preferences")
            return {'CANCELLED'}
        
        def submit_upscale():
            try:
                print("Starting upscale submission...")
                image = texture_node.image
                temp_path = os.path.join(bpy.app.tempdir, f"temp_{image.name}")
                print(f"Saving temp file to: {temp_path}")
                
                image.save_render(temp_path)
                
                if not os.path.exists(temp_path):
                    print(f"Failed to save temporary file: {temp_path}")
                    self._queue.put(None)
                    return
                
                headers = {"Authorization": f"Bearer {addon_prefs.api_key}"}
                with open(temp_path, 'rb') as f:
                    files = {'content': (os.path.basename(temp_path), f, 'image/png')}
                    print("Uploading file to Replicate...")
                    upload_response = requests.post(
                        "https://api.replicate.com/v1/files",
                        headers=headers,
                        files=files
                    )
                
                if upload_response.status_code != 201:
                    print(f"Upload failed with status {upload_response.status_code}: {upload_response.text}")
                    self._queue.put(None)
                    return
                
                image_url = upload_response.json()['urls']['get']
                print(f"File uploaded, got URL: {image_url}")
                
                data = {
                    "version": "f121d640bd286e1fdc67f9799164c1d5be36ff74576ee11c803ae5b665dd46aa",
                    "input": {
                        "image": image_url,
                        "scale": float(context.scene.ai_texture_props.upscale_factor),
                        "face_enhance": bool(context.scene.ai_texture_props.face_enhance)
                    }
                }
                
                print(f"Submitting prediction with data: {data}")
                response = requests.post(
                    "https://api.replicate.com/v1/predictions",
                    json=data,
                    headers=headers
                )
                
                if response.status_code == 201:
                    prediction_id = response.json()['id']
                    print(f"Prediction submitted, ID: {prediction_id}")
                    self._queue.put(prediction_id)
                else:
                    print(f"Prediction submission failed: {response.text}")
                    self._queue.put(None)
                
                try:
                    os.remove(temp_path)
                except Exception as e:
                    print(f"Warning: Could not remove temp file: {e}")
                
            except Exception as e:
                print(f"Error in submit_upscale: {str(e)}")
                self._queue.put(None)
        
        self._thread = Thread(target=submit_upscale)
        self._thread.start()
        
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.5, window=context.window)
        wm.modal_handler_add(self)
        
        return {'RUNNING_MODAL'}
    
    def cancel(self, context):
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

def register():
    bpy.utils.register_class(AIModelSettings)
    bpy.utils.register_class(AITextureProperties)
    bpy.utils.register_class(AITextureGeneratorPreferences)
    bpy.utils.register_class(AITextureGenerator)
    bpy.utils.register_class(AITextureGeneratorPanel)
    bpy.utils.register_class(AITextureDelete)
    bpy.utils.register_class(AITextureUpdate)
    bpy.utils.register_class(AITextureSelect)
    bpy.utils.register_class(AITextureAssign)
    bpy.utils.register_class(AITextureUpscale)
    
    bpy.types.Scene.ai_texture_generator_text_prompt = StringProperty(
        name="Text Prompt",
        description="Describe the texture you want to generate",
        default="",
        maxlen=1024,
        subtype='NONE'
    )
    bpy.types.Scene.progress_status = StringProperty(
        name="Progress Status",
        description="Current status of the texture generation process",
        default="Waiting..."
    )
    bpy.types.Scene.ai_texture_props = bpy.props.PointerProperty(type=AITextureProperties)
    bpy.types.Scene.ai_model_settings = bpy.props.PointerProperty(type=AIModelSettings)

def unregister():
    bpy.utils.unregister_class(AIModelSettings)
    bpy.utils.unregister_class(AITextureProperties)
    bpy.utils.unregister_class(AITextureGeneratorPreferences)
    bpy.utils.unregister_class(AITextureGenerator)
    bpy.utils.unregister_class(AITextureGeneratorPanel)
    bpy.utils.unregister_class(AITextureDelete)
    bpy.utils.unregister_class(AITextureUpdate)
    bpy.utils.unregister_class(AITextureSelect)
    bpy.utils.unregister_class(AITextureAssign)
    bpy.utils.unregister_class(AITextureUpscale)
    
    del bpy.types.Scene.ai_texture_generator_text_prompt
    del bpy.types.Scene.progress_status
    del bpy.types.Scene.ai_texture_props
    del bpy.types.Scene.ai_model_settings

if __name__ == "__main__":
    register()