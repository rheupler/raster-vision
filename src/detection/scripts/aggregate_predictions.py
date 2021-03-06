import json
import argparse
from os import makedirs
from os.path import join

import numpy as np
from scipy.misc import imsave
import matplotlib as mpl
mpl.use('Agg') # NOQA
from cv2 import groupRectangles
import rasterio

from object_detection.utils import label_map_util
from object_detection.utils import visualization_utils as vis_util

from settings import max_num_classes, line_thickness, planet_channel_order
from utils import load_window


def compute_agg_predictions(window_offsets, window_size, im_size, predictions):
    ''' Aggregate window predictions into predictions for original image. '''
    boxes = []
    scores = []
    classes = []

    file_names = sorted(predictions.keys())
    for file_name in file_names:
        preds = predictions[file_name]
        x, y = window_offsets[file_name]

        for box in preds['boxes']:
            # box is (ymin, xmin, ymax, xmax) in relative coords
            # (eg. 0.5 is middle of axis).
            # x, y are in pixel offsets.
            box = np.array(box) * window_size

            box[0] += y  # ymin
            box[1] += x  # xmin
            box[2] += y  # ymax
            box[3] += x  # xmax

            # Coordinates are floats between 0 and 1.
            box[0] /= im_size[1]
            box[1] /= im_size[0]
            box[2] /= im_size[1]
            box[3] /= im_size[0]

            box = np.clip(box, 0, 1).tolist()
            boxes.append(box)

        scores.extend(preds['scores'])
        classes.extend([int(class_id) for class_id in preds['classes']])

    return boxes, scores, classes


def plot_predictions(plot_path, im, category_index, boxes, scores, classes):
    min_val = np.min(im)
    max_val = np.max(im)
    norm_im = 256 * ((im - min_val) / (max_val - min_val))
    norm_im = norm_im.astype(np.uint8)

    vis_util.visualize_boxes_and_labels_on_image_array(
        norm_im,
        np.squeeze(boxes),
        np.squeeze(classes).astype(np.int32),
        np.squeeze(scores),
        category_index,
        use_normalized_coordinates=True,
        line_thickness=line_thickness)

    imsave(plot_path, norm_im)


def box_to_cv2_rect(im_size, box):
    ymin, xmin, ymax, xmax = box
    width = xmax - xmin
    height = ymax - ymin

    xmin = int(xmin * im_size[0])
    width = int(width * im_size[0])
    ymin = int(ymin * im_size[1])
    height = int(height * im_size[1])

    rect = (xmin, ymin, width, height)
    return rect


def cv2_rect_to_box(im_size, rect):
    x, y, width, height = rect

    x /= im_size[0]
    width /= im_size[0]
    y /= im_size[1]
    height /= im_size[1]

    box = [y, x, y + height, x + width]
    return box


def compute_overlap_score(boxA, boxB):
    # determine the (x, y)-coordinates of the intersection rectangle
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])

    # compute the area of intersection rectangle
    interArea = (xB - xA) * (yB - yA)

    # compute the area of both the prediction and ground-truth
    # rectangles
    boxAArea = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1])
    boxBArea = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1])

    return max(interArea / boxAArea, interArea / boxBArea)


def overlaps(box1, box2):
    return not (
        box1[2] < box2[0] or  # left is to the left of right
        box1[0] > box2[0] or  # left is to the right of right
        box1[3] < box2[1] or  # bottom is above the top
        box1[1] > box2[3])  # top is below the bottom


def rect_to_bbox(rect):
    x, y, width, height = rect
    return [x, y, x + width, y + height]


def group_boxes(boxes, scores, im_size, nb_passes=1):
    '''Group boxes belonging to a single class.'''
    box_to_score = dict(zip(boxes, scores))
    threshold = 0.5

    grouped_boxes = set(boxes)

    for pass_ind in range(nb_passes):
        for box in boxes:
            # Find other box that overlaps the most with box.
            max_overlap_score = 0.0
            max_box = None
            for other_box in grouped_boxes:
                if box != other_box and overlaps(box, other_box):
                    overlap_score = compute_overlap_score(box, other_box)
                    if overlap_score > max_overlap_score:
                        max_overlap_score = overlap_score
                        max_box = other_box

            # If overlaps enough, then replace box and other_box with
            # the box with the highest score (probability of detection).
            if max_overlap_score > threshold:
                box_score = box_to_score[box]
                max_box_score = box_to_score[max_box]

                if max_box in grouped_boxes:
                    grouped_boxes.remove(max_box)
                if box in grouped_boxes:
                    grouped_boxes.remove(box)

                if box_score > max_box_score:
                    grouped_boxes.add(box)
                else:
                    grouped_boxes.add(max_box)

    grouped_boxes = list(grouped_boxes)
    grouped_scores = [box_to_score[box] for box in grouped_boxes]

    return grouped_boxes, grouped_scores


def group_predictions(boxes, classes, scores, im_size):
    '''For each class, group boxes that are overlapping.'''
    unique_classes = list(set(classes))

    grouped_boxes = []
    grouped_classes = []
    grouped_scores = []

    for class_id in unique_classes:
        class_boxes = []
        class_scores = []
        for ind, a_class_id in enumerate(classes):
            if class_id == a_class_id:
                class_boxes.append(tuple(boxes[ind]))
                class_scores.append(scores[ind])

        class_grouped_boxes, class_grouped_scores = \
            group_boxes(class_boxes, class_scores, im_size, nb_passes=10)

        grouped_boxes.extend(class_grouped_boxes)
        grouped_scores.extend(class_grouped_scores)
        grouped_classes.extend([class_id] * len(class_grouped_boxes))

    return grouped_boxes, grouped_classes, grouped_scores


def save_geojson(path, boxes, classes, scores, im_size, category_index,
                 image_dataset=None):
    polygons = []
    for box in boxes:
        x, y, width, height = box_to_cv2_rect(im_size, box)
        nw = (x, y)
        ne = (x + width, y)
        se = (x + width, y + height)
        sw = (x, y + height)
        polygon = [nw, ne, se, sw, nw]
        # Transform from pixel coords to spatial coords
        if image_dataset:
            polygon = [image_dataset.ul(point[1], point[0])
                       for point in polygon]
        polygons.append(polygon)

    crs = None
    if image_dataset:
        # XXX not sure if I'm getting this properly
        crs_name = image_dataset.crs['init']
        crs = {
            'type': 'name',
            'properties': {
                'name': crs_name
            }
        }

    features = [{
            'type': 'Feature',
            'properties': {
                'class_id': int(class_id),
                'class_name': category_index[class_id]['name'],
                'score': score

            },
            'geometry': {
                'type': 'Polygon',
                'coordinates': [polygon]
            }
        }
        for polygon, class_id, score in zip(polygons, classes, scores)
    ]

    geojson = {
        'type': 'FeatureCollection',
        'crs': crs,
        'features': features
    }

    with open(path, 'w') as json_file:
        json.dump(geojson, json_file, indent=4)


def aggregate_predictions(image_path, window_info_path, predictions_path,
                          label_map_path, output_dir, channel_order,
                          debug=False):
    print('Aggregating predictions over windows...')

    label_map = label_map_util.load_labelmap(label_map_path)
    categories = label_map_util.convert_label_map_to_categories(
        label_map, max_num_classes=max_num_classes, use_display_name=True)
    category_index = label_map_util.create_category_index(categories)

    image_dataset = rasterio.open(image_path)
    im_size = [image_dataset.width, image_dataset.height]

    with open(window_info_path) as window_info_file:
        window_info = json.load(window_info_file)
        window_offsets = window_info['offsets']
        window_size = window_info['window_size']

    with open(predictions_path) as predictions_file:
        predictions = json.load(predictions_file)

    makedirs(output_dir, exist_ok=True)
    boxes, scores, classes = compute_agg_predictions(
        window_offsets, window_size, im_size, predictions)
    # Due to the sliding window approach, sometimes there are multiple
    # slightly different detections where there should only be one. So
    # we group them together.
    boxes, classes, scores = group_predictions(boxes, classes, scores, im_size)

    agg_predictions_path = join(output_dir, 'predictions.geojson')
    save_geojson(agg_predictions_path, boxes, classes, scores, im_size,
                 category_index, image_dataset=image_dataset)

    if debug:
        im = load_window(image_dataset, channel_order)
        plot_path = join(output_dir, 'predictions.png')
        plot_predictions(plot_path, im, category_index, boxes, scores, classes)


def parse_args():
    description = """
        Aggregate predictions from windows into predictions over original
        image. The output is GeoJSON in the CRS of the input image.
    """
    parser = argparse.ArgumentParser(description=description)

    parser.add_argument('--image-path', help='Path to TIFF or VRT file')
    parser.add_argument('--window-info-path')
    parser.add_argument('--predictions-path')
    parser.add_argument('--label-map-path')
    parser.add_argument('--output-dir')
    parser.add_argument('--channel-order', nargs=3, type=int,
                        default=planet_channel_order)
    parser.add_argument('--debug', dest='debug', action='store_true')

    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    print(args)

    aggregate_predictions(
        args.image_path, args.window_info_path, args.predictions_path,
        args.label_map_path, args.output_dir, args.channel_order, args.debug)
