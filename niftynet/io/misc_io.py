# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, unicode_literals

import logging as log
import os
import re
import sys
import warnings

import PIL
import nibabel as nib
import numpy as np
import scipy.ndimage
import tensorflow as tf
from PIL.GifImagePlugin import Image as GIF
from tensorflow.core.framework import summary_pb2

IS_PYTHON2 = False if sys.version_info[0] > 2 else True

IMAGE_LOADERS = [nib.load]
try:
    import niftynet.io.simple_itk_as_nibabel

    IMAGE_LOADERS.append(
        niftynet.io.simple_itk_as_nibabel.SimpleITKAsNibabel)
except ImportError:
    warnings.warn(
        'SimpleITK adapter failed to load,'
        ' reducing the supported file formats.',
        ImportWarning)

warnings.simplefilter("ignore", UserWarning)

FILE_EXTENSIONS = [".nii.gz", ".tar.gz"]
CONSOLE_LOG_FORMAT = '%(levelname)s:niftynet: %(message)s'
FILE_LOG_FORMAT = '%(levelname)s:niftynet:%(asctime)s: %(message)s'


#### utilities for file headers

def infer_ndims_from_file(file_path):
    image_header = load_image(file_path).header
    try:
        ndims = int(image_header['dim'][0])
    except IndexError:
        tf.logging.fatal('unsupported file header in: {}'.format(file_path))
        raise
    return ndims


def create_affine_pixdim(affine, pixdim):
    """
    Given an existing affine transformation and the pixel dimension to apply,
    create a new affine matrix that satisfies the new pixel dimension
    :param affine: original affine matrix
    :param pixdim: pixel dimensions to apply
    :return:
    """
    norm_affine = np.sqrt(np.sum(np.square(affine[:, 0:3]), 0))
    to_divide = np.tile(
        np.expand_dims(np.append(norm_affine, 1), axis=1), [1, 4])
    to_multiply = np.tile(
        np.expand_dims(np.append(np.asarray(pixdim), 1), axis=1), [1, 4])
    return np.multiply(np.divide(affine, to_divide.T), to_multiply.T)


def load_image(filename):
    # load an image from a supported filetype and return an object
    # that matches nibabel's spatialimages interface
    for image_loader in IMAGE_LOADERS:
        try:
            img = image_loader(filename)
            img = correct_image_if_necessary(img)
            return img
        except nib.filebasedimages.ImageFileError:
            # if the image_loader cannot handle the type_str
            # continue to next loader
            pass
    raise nib.filebasedimages.ImageFileError(
        'No loader could load the file {}'.format(filename))


def correct_image_if_necessary(img):
    if img.header['dim'][0] == 5:
        # do nothing for high-dimensional array
        return img
    # Check that affine matches zooms
    pixdim = img.header.get_zooms()
    if not np.array_equal(
            np.sqrt(np.sum(np.square(img.affine[0:3, 0:3]), 0)),
            np.asarray(pixdim)):
        if hasattr(img, 'get_sform'):
            # assume it is a malformed NIfTI and try to fix it
            img = rectify_header_sform_qform(img)
    return img


def rectify_header_sform_qform(img_nii):
    """
    Look at the sform and qform of the nifti object and
    correct it if any incompatibilities with pixel dimensions
    :param img_nii:
    :return:
    """
    # TODO: check img_nii is a nibabel object
    pixdim = img_nii.header.get_zooms()
    sform = img_nii.get_sform()
    qform = img_nii.get_qform()
    norm_sform = np.sqrt(np.sum(np.square(sform[0:3, 0:3]), 0))
    norm_qform = np.sqrt(np.sum(np.square(qform[0:3, 0:3]), 0))
    flag_sform_problem = False
    flag_qform_problem = False
    if not np.array_equal(norm_sform, np.asarray(pixdim)):
        flag_sform_problem = True
    if not np.array_equal(norm_qform, np.asarray(pixdim)):
        flag_qform_problem = True

    if img_nii.header['sform_code'] > 0:
        if not flag_sform_problem:
            return img_nii
        elif not flag_qform_problem:
            # recover by copying the qform over the sform
            img_nii.set_sform(np.copy(img_nii.get_qform()))
            return img_nii
    elif img_nii.header['qform_code'] > 0:
        if not flag_qform_problem:
            return img_nii
        elif not flag_sform_problem:
            # recover by copying the sform over the qform
            img_nii.set_qform(np.copy(img_nii.get_sform()))
            return img_nii
    affine = img_nii.affine
    pixdim = img_nii.header.get_zooms()
    while len(pixdim) < 3:
        pixdim = pixdim + (1.0,)
    # TODO: assuming 3 elements
    new_affine = create_affine_pixdim(affine, pixdim[:3])
    img_nii.set_sform(new_affine)
    img_nii.set_qform(new_affine)
    return img_nii


#### end of utilities for file headers


### resample/reorientation original volumes
# Perform the reorientation to ornt_fin of the data array given ornt_init
def do_reorientation(data_array, init_axcodes, final_axcodes):
    """
    Performs the reorientation (changing order of axes)
    :param data_array: Array to reorient
    :param ornt_init: Initial orientation
    :param ornt_fin: Target orientation
    :return data_reoriented: New data array in its reoriented form
    """
    ornt_init = nib.orientations.axcodes2ornt(init_axcodes)
    ornt_fin = nib.orientations.axcodes2ornt(final_axcodes)
    if np.array_equal(ornt_init, ornt_fin):
        return data_array
    if np.any(np.isnan(ornt_init)) or np.any(np.isnan(ornt_fin)):
        tf.logging.fatal("unknown axcodes %s, %s", ornt_init, ornt_fin)
        raise ValueError
    try:
        ornt_transf = nib.orientations.ornt_transform(ornt_init, ornt_fin)
        data_reoriented = nib.orientations.apply_orientation(
            data_array, ornt_transf)
    except (ValueError, IndexError):
        tf.logging.fatal('reorientation undecided %s, %s', ornt_init, ornt_fin)
        raise ValueError
    return data_reoriented


# Perform the resampling of the data array given the initial and final pixel
# dimensions and the interpolation order
# this function assumes the same interp_order for multi-modal images
# do we need separate interp_order for each modality?
def do_resampling(data_array, pixdim_init, pixdim_fin, interp_order):
    """
    Performs the resampling
    :param data_array: Data array to resample
    :param pixdim_init: Initial pixel dimension
    :param pixdim_fin: Targeted pixel dimension
    :param interp_order: Interpolation order applied
    :return data_resampled: Array containing the resampled data
    """
    if data_array is None:
        return
    if np.array_equal(pixdim_fin, pixdim_init):
        return data_array
    try:
        assert len(pixdim_init) <= len(pixdim_fin)
    except (TypeError, AssertionError):
        tf.logging.fatal("unkown pixdim format original %s output %s",
                         pixdim_init, pixdim_fin)
        raise
    to_multiply = np.divide(pixdim_init, pixdim_fin[:len(pixdim_init)])
    data_shape = data_array.shape
    if len(data_shape) != 5:
        raise ValueError("only supports 5D array resampling, "
                         "input shape {}".format(data_shape))
    data_resampled = []
    for t in range(0, data_shape[3]):
        data_mod = []
        for m in range(0, data_shape[4]):
            data_new = scipy.ndimage.zoom(data_array[..., t, m],
                                          to_multiply[0:3],
                                          order=interp_order)
            data_mod.append(data_new[..., np.newaxis, np.newaxis])
        data_resampled.append(np.concatenate(data_mod, axis=-1))
    return np.concatenate(data_resampled, axis=-2)


### end of resample/reorientation original volumes

def save_data_array(filefolder,
                    filename,
                    array_to_save,
                    image_object=None,
                    interp_order=3,
                    reshape=True):
    """
    write image data array to hard drive using image_object
    properties such as affine, pixdim and axcodes.
    """
    if image_object is not None:
        affine = image_object.original_affine[0]
        image_pixdim = image_object.output_pixdim[0]
        image_axcodes = image_object.output_axcodes[0]
        dst_pixdim = image_object.original_pixdim[0]
        dst_axcodes = image_object.original_axcodes[0]
    else:
        affine = np.eye(4)
        image_pixdim, image_axcodes = (), ()

    if reshape:
        input_ndim = array_to_save.ndim
        if input_ndim == 1:
            # feature vector, should be saved with shape (1, 1, 1, 1, mod)
            while array_to_save.ndim < 5:
                array_to_save = np.expand_dims(array_to_save, axis=0)
        elif input_ndim == 2 or input_ndim == 3:
            # 2D or 3D images should be saved with shape (x, y, z, 1, 1)
            while array_to_save.ndim < 5:
                array_to_save = np.expand_dims(array_to_save, axis=-1)
        elif input_ndim == 4:
            # recover a time dimension for nifti format output
            array_to_save = np.expand_dims(array_to_save, axis=3)

    if image_pixdim:
        array_to_save = do_resampling(
            array_to_save, image_pixdim, dst_pixdim, interp_order)
    if image_axcodes:
        array_to_save = do_reorientation(
            array_to_save, image_axcodes, dst_axcodes)
    save_volume_5d(array_to_save, filename, filefolder, affine)


def expand_to_5d(img_data):
    """
    Expands an array up to 5d if it is not the case yet
    :param img_data:
    :return:
    """
    while img_data.ndim < 5:
        img_data = np.expand_dims(img_data, axis=-1)
    return img_data


def save_volume_5d(img_data, filename, save_path, affine=np.eye(4)):
    """
    Save the img_data to nifti image
    :param img_data: 5d img to save
    :param filename: filename under which to save the img_data
    :param save_path:
    :param affine: an affine matrix.
    :return:
    """
    if img_data is None:
        return
    touch_folder(save_path)
    img_nii = nib.Nifti1Image(img_data, affine)
    # img_nii.set_data_dtype(np.dtype(np.float32))
    output_name = os.path.join(save_path, filename)
    try:
        nib.save(img_nii, output_name)
    except OSError:
        tf.logging.fatal("writing failed {}".format(output_name))
        raise
    print('Saved {}'.format(output_name))


def split_filename(file_name):
    pth = os.path.dirname(file_name)
    fname = os.path.basename(file_name)

    ext = None
    for special_ext in FILE_EXTENSIONS:
        ext_len = len(special_ext)
        if fname[-ext_len:].lower() == special_ext:
            ext = fname[-ext_len:]
            fname = fname[:-ext_len] if len(fname) > ext_len else ''
            break
    if not ext:
        fname, ext = os.path.splitext(fname)
    return pth, fname, ext


def squeeze_spatial_temporal_dim(tf_tensor):
    """
    Given a tensorflow tensor, ndims==6 means:
    [batch, x, y, z, time, modality]
    this function removes x, y, z, and time dims if
    the length along the dims is one
    :return: squeezed tensor
    """
    if tf_tensor.get_shape().ndims != 6:
        return tf_tensor
    if tf_tensor.get_shape()[4] != 1:
        raise NotImplementedError("time sequences not currently supported")
    axis_to_squeeze = []
    for (idx, axis) in enumerate(tf_tensor.shape.as_list()):
        if idx == 0 or idx == 5:
            continue
        if axis == 1:
            axis_to_squeeze.append(idx)
    return tf.squeeze(tf_tensor, axis=axis_to_squeeze)


def touch_folder(model_dir):
    """
    This funciton returns the absolute path of `model_dir` if exists
    otherwise try to create the folder and returns the absolute path
    """
    if not os.path.exists(model_dir):
        try:
            os.makedirs(model_dir)
        except OSError:
            tf.logging.fatal('could not create model folder: %s', model_dir)
            raise OSError
    absolute_dir = os.path.abspath(model_dir)
    # tf.logging.info('accessing output folder: {}'.format(absolute_dir))
    return absolute_dir


def resolve_checkpoint(checkpoint_name):
    # For now only supports checkpoint_name where
    # checkpoint_name.index is in the file system
    # eventually will support checkpoint names that can be referenced
    # in a paths file
    if os.path.isfile(checkpoint_name + '.index'):
        return checkpoint_name
    raise ValueError('Invalid checkpoint {}'.format(checkpoint_name))


def get_latest_subfolder(parent_folder, create_new=False):
    parent_folder = touch_folder(parent_folder)
    try:
        log_sub_dirs = os.listdir(parent_folder)
    except OSError:
        tf.logging.fatal('not a directory {}'.format(parent_folder))
        raise OSError
    log_sub_dirs = [name for name in log_sub_dirs
                    if re.findall('^[0-9]+$', name)]
    if log_sub_dirs and create_new:
        latest_id = max([int(name) for name in log_sub_dirs])
        log_sub_dir = str(latest_id + 1)
    elif log_sub_dirs and not create_new:
        latest_valid_id = max(
            [int(name) for name in log_sub_dirs
             if os.path.isdir(os.path.join(parent_folder, name))])
        log_sub_dir = str(latest_valid_id)
    else:
        log_sub_dir = '0'
    return os.path.join(parent_folder, log_sub_dir)


def _image3_animated_gif(tag, ims):
    # x=numpy.random.randint(0,256,[10,10,10],numpy.uint8)
    ims = [np.asarray((ims[i, :, :]).astype(np.uint8))
           for i in range(ims.shape[0])]
    ims = [GIF.fromarray(im) for im in ims]
    s = b''
    for b in PIL.GifImagePlugin.getheader(ims[0])[0]:
        s += b
    s += b'\x21\xFF\x0B\x4E\x45\x54\x53\x43\x41\x50' \
         b'\x45\x32\x2E\x30\x03\x01\x00\x00\x00'
    for i in ims:
        for b in PIL.GifImagePlugin.getdata(i):
            s += b
    s += b'\x3B'
    if IS_PYTHON2:
        s = str(s)
    summary_image_str = summary_pb2.Summary.Image(
        height=10, width=10, colorspace=1, encoded_image_string=s)
    image_summary = summary_pb2.Summary.Value(
        tag=tag, image=summary_image_str)
    return [summary_pb2.Summary(value=[image_summary]).SerializeToString()]


def image3(name,
           tensor,
           max_outputs=3,
           collections=[tf.GraphKeys.SUMMARIES],
           animation_axes=[1],
           image_axes=[2, 3],
           other_indices={}):
    """ Summary for higher dimensional images
    Parameters:
    name: string name for the summary
    tensor:   tensor to summarize. Should be in the range 0..255.
              By default, assumes tensor is NDHWC, and animates (through D)
              HxW slices of the 1st channel.
    collections: list of strings collections to add the summary to
    animation_axes=[1],image_axes=[2,3]
    """
    if max_outputs == 1:
        suffix = '/image'
    else:
        suffix = '/image/{}'
    axis_order = [0] + animation_axes + image_axes
    # slice tensor
    slicing = []
    for i in range(len(tensor.shape)):
        if i in axis_order:
            slicing.append(slice(None))
        else:
            other_ind = other_indices.get(i, 0)
            slicing.append(slice(other_ind, other_ind + 1))
    slicing = tuple(slicing)
    tensor = tensor[slicing]
    axis_order_all = \
        axis_order + [i for i in range(len(tensor.shape.as_list()))
                      if i not in axis_order]
    original_shape = tensor.shape.as_list()
    new_shape = [original_shape[0], -1,
                 original_shape[axis_order[-2]],
                 original_shape[axis_order[-1]]]
    transposed_tensor = tf.transpose(tensor, axis_order_all)
    transposed_tensor = tf.reshape(transposed_tensor, new_shape)
    # split images
    with tf.device('/cpu:0'):
        for it in range(min(max_outputs, transposed_tensor.shape.as_list()[0])):
            inp = [name + suffix.format(it), transposed_tensor[it, :, :, :]]
            summary_op = tf.py_func(_image3_animated_gif, inp, tf.string)
            for c in collections:
                tf.add_to_collection(c, summary_op)
    return summary_op


def image3_sagittal(name,
                    tensor,
                    max_outputs=3,
                    collections=[tf.GraphKeys.SUMMARIES]):
    return image3(name, tensor, max_outputs, collections, [1], [2, 3])


def image3_coronal(name,
                   tensor,
                   max_outputs=3,
                   collections=[tf.GraphKeys.SUMMARIES]):
    return image3(name, tensor, max_outputs, collections, [2], [1, 3])


def image3_axial(name,
                 tensor,
                 max_outputs=3,
                 collections=[tf.GraphKeys.SUMMARIES]):
    return image3(name, tensor, max_outputs, collections, [3], [1, 2])


def set_logger(file_name=None):
    tf.logging._logger.handlers = []
    tf.logging._logger = log.getLogger('tensorflow')
    tf.logging.set_verbosity(tf.logging.INFO)

    f = log.Formatter(CONSOLE_LOG_FORMAT)
    std_handler = log.StreamHandler(sys.stdout)
    std_handler.setFormatter(f)
    tf.logging._logger.addHandler(std_handler)

    if file_name is not None:
        f = log.Formatter(FILE_LOG_FORMAT)
        file_handler = log.FileHandler(file_name)
        file_handler.setFormatter(f)
        tf.logging._logger.addHandler(file_handler)
