#!/usr/bin/env python3

# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Take a folder, write a TFRecord into it
# https://github.com/tensorflow/models/blob/master/research/object_detection/g3doc/using_your_own_dataset.md

import functools
import time
import random
import sys
import collections
import multiprocessing
import multiprocessing.pool
import io
from PIL import Image
import numpy as np
import os
import argparse
import bbox_writer
import tensorflow as tf

description_text = """\
Use this script to convert labeled images into TFRecord files for training.

Both scripts in the labeling pipeline (labeler.py, tracking.py) store annotated
images as an image with a corresponding .txt file containing labels. While that
format is convenient for human readability, it is not the format TensorFlow is
expecting. This script converts the paired image format to the TFRecord format
that TensorFlow is expecting. Furthermore, this script can automatically
generate an evaluation split, or create one from a separate folder. Finally, the
label map required for the Object Detection API is generated and placed into the
specified folder.
"""

epilog_text = """\
example:
    ./convert_labels_to_records.py [folder]               convert without eval
    ./convert_labels_to_records.py [folder] --eval        generate an eval split
"""

parser = argparse.ArgumentParser(
        description=description_text,
        epilog=epilog_text,
        formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("folder", type=str,
        help="Folder containing training data. All converted data output here.")
parser.add_argument("-n", "--number", type=int, default=10,
        help="Number of records")
parser.add_argument("-s", "--split", type=float, default=0.15,
        help="Percentage of training data to use for eval. Ignored if"
        " --eval_folder is specified.")

eval_options = parser.add_mutually_exclusive_group()
eval_options.add_argument("-e", "--eval", action="store_true", default=False,
        help="Automatically generate eval split from train folder")
eval_options.add_argument("--eval_folder", type=str, default=None,
        help="Folder containing eval data")

args = parser.parse_args()

# NamedTuple to store results from example writing workers
Result = collections.namedtuple("RecordWritingResult",
        ["id", "record_count", "class_count", "negative_count"])

def create_tf_example(labels, txt_full_path):

    # Check to see whether the first line is the path or not.
    with open(txt_full_path, "r") as f:
        first = f.readline().strip()
        if first.startswith("#") and os.path.isfile(first[1:]): # On disk
            print("Found filename %s in the first line!" % first[1:])
            image_full_path = first[1:]
        else:
            txt_name = os.path.basename(txt_full_path)
            image_name = os.path.splitext(txt_name)[0] + ".png"
            image_full_path = os.path.join(os.path.dirname(txt_full_path),
                                           image_name)

    im = Image.open(image_full_path)
    arr = io.BytesIO()
    im.save(arr, format='PNG')

    height = im.height # Image height
    width = im.width # Image width
    channels = np.array(im).shape[2]
    filename = image_full_path # Filename of the image.
    encoded_image_data = arr.getvalue() # Encoded image bytes in png
    image_fmt = 'png' # Compression for the image bytes (encoded_image_data)

    rects, classes = bbox_writer.read_rects(txt_full_path)
    # List of normalized coordinates, 1 per box, capped to [0, 1]
    xmins = [max(min(rect[0] / width, 1), 0) for rect in rects] # left x
    xmaxs = [max(min(rect[2] / width, 1), 0) for rect in rects] # right x
    ymins = [max(min(rect[1] / height, 1), 0) for rect in rects] # top y
    ymaxs = [max(min(rect[3] / height, 1), 0) for rect in rects] # bottom y

    classes_txt = [cls.encode('utf-8') for cls in classes] # String names
    class_ids = [labels[cls] for cls in classes]

    tf_example = tf.train.Example(features=tf.train.Features(feature={
        'image/height': int64_feature(height),
        'image/width': int64_feature(width),
        'image/channels': int64_feature(channels),
        'image/colorspace': bytes_feature("RGB".encode('utf-8')),
        'image/filename': bytes_feature(filename.encode('utf-8')),
        'image/source_id': bytes_feature(filename.encode('utf-8')),
        'image/image_key': bytes_feature(filename.encode('utf-8')),
        'image/encoded': bytes_feature(encoded_image_data),
        'image/format': bytes_feature(image_fmt.encode('utf-8')),
        'image/object/bbox/xmin': float_list_feature(xmins),
        'image/object/bbox/xmax': float_list_feature(xmaxs),
        'image/object/bbox/ymin': float_list_feature(ymins),
        'image/object/bbox/ymax': float_list_feature(ymaxs),
        'image/object/class/text': bytes_list_feature(classes_txt),
        'image/object/class/label': int64_list_feature(class_ids),
    }))

    is_negative = len(rects) == 0

    return tf_example, collections.Counter(classes), is_negative


def write_record_from_list(id, labels, data, out_path):

    examples_count = collections.Counter()
    negative_count = 0
    with tf.io.TFRecordWriter(out_path) as writer:
        for filename in data:
            print("[%s] Writing record for" % id, filename)
            example, count, is_negative = create_tf_example(labels, filename)
            writer.write(example.SerializeToString())
            examples_count += count
            negative_count += is_negative

    return Result(id, len(data), examples_count, negative_count)


def get_shuffled_filenames(folder):
    # Grab the names of all of the pieces of train data
    train_txts = []
    for root, dirs, files in os.walk(folder):
        for name in files:
            if not name.endswith(".txt"): continue
            if name.endswith("rects.txt"): continue # ignore init files

            txt_name = name
            txt_full_path = os.path.join(root, txt_name)
            train_txts.append(txt_full_path)

    random.shuffle(train_txts) # So that we get a spread of eval data
    return train_txts


def get_labels_from_filenames(filenames):

    labels = set()
    for filename in filenames:
        _, classes = bbox_writer.read_rects(filename)
        labels.update(set(classes))

    # Should be stable now
    labels = sorted(labels)
    return {label:i for i,label in enumerate(labels)}


def get_train_eval_split_filenames(filenames, split):

    eval_size = int(len(filenames) * split)
    print("Generating eval split of %d elements" % eval_size)

    eval_filenames = filenames[:eval_size]
    train_filenames = filenames[eval_size:]

    return train_filenames, eval_filenames


def write_labels(folder, labels):

    # Write out the label map file to disk
    label_name = os.path.join(folder, "label.pbtxt")
    label_txt = []
    for cls, i in labels.items():
        label_txt.append("item {\n  id: %d\n  name:'%s'\n}\n" % (i + 1, cls))

    with open(label_name, "w") as f:
        f.write("\n".join(label_txt))

    print("Wrote %d labels: %s" % (len(labels), labels))


def print_results(results):

    example_counts = collections.Counter()
    record_counts = 0
    negative_counts = 0
    for result in results:
        print("[%s] has %d examples, %s split" %
                (result.id, result.record_count, result.class_count))
        record_counts += result.record_count
        example_counts += result.class_count
        negative_counts += result.negative_count

    print("Overall records:", record_counts)
    print("Overall examples:", example_counts)
    print("Overall negatives:", negative_counts)


def get_record_writing_tasks():
    """Assign all record writing work into different lists.

    This function parses flags to determine training and eval splits. Eval data
    will either not be generated at all (neither --eval nor --eval_folder
    specified), generated automatically from train data (--eval), or sourced
    from a separate location (--eval_folder).
    """

    train_filenames = get_shuffled_filenames(args.folder)
    print("Got train filenames", len(train_filenames))

    if args.eval_folder is not None:
        print("Getting filenames from eval folder")
        eval_filenames = get_shuffled_filenames(args.eval_folder)
    elif args.eval:
        print("Splitting train to get eval")
        train_filenames, eval_filenames = get_train_eval_split_filenames(
                train_filenames, args.split)
        print("Got new train filenames", len(train_filenames))
    else: # No eval at all
        print("Not generating an eval set")
        eval_filenames = []

    print("Got eval filenames", len(eval_filenames))

    labels = get_labels_from_filenames(train_filenames + eval_filenames)
    print("Generated labels:", labels)

    tasks = []

    # Generate the training tasks by splitting up the train filenames
    train_record_number = min(max(args.number, 1), len(train_filenames))
    train_lists = [[] for i in range(train_record_number)]
    for i, filename in enumerate(train_filenames):
        train_lists[i % train_record_number].append(filename)

    for i, train_list in enumerate(train_lists):
        out_path = os.path.join(args.folder, "train-%02d.record" % i)
        tasks.append(("train-%02d" % i, labels, train_list, out_path))

    # Generate eval tasks by splitting up the eval filenames
    eval_record_number = min(max(args.number, 1), len(eval_filenames))
    eval_lists = [[] for i in range(eval_record_number)]
    for i, filename in enumerate(eval_filenames):
        eval_lists[i % eval_record_number].append(filename)

    for i, eval_list in enumerate(eval_lists):
        out_path = os.path.join(args.folder, "eval-%02d.record" % i)
        tasks.append(("eval-%02d" % i, labels, eval_list, out_path))

    return tasks, labels


def int64_feature(value):
  return tf.train.Feature(int64_list=tf.train.Int64List(value=[value]))


def int64_list_feature(value):
  return tf.train.Feature(int64_list=tf.train.Int64List(value=value))


def bytes_feature(value):
  return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))


def bytes_list_feature(value):
  return tf.train.Feature(bytes_list=tf.train.BytesList(value=value))


def float_list_feature(value):
  return tf.train.Feature(float_list=tf.train.FloatList(value=value))


if __name__ == "__main__":
    t_start = time.time()

    random.seed(42) # Make sure the shuffle order is the same

    # Make the tasks for the workers to process
    tasks, labels = get_record_writing_tasks()

    print(len(tasks))

    # Actually have the workers generate the records
    pool = multiprocessing.pool.ThreadPool()
    results = pool.starmap(write_record_from_list, tasks)

    # Write out the labels at the end so the summary gets printed here
    write_labels(args.folder, labels)
    print_results(results)

    t_end = time.time()
    print("Took %5.2fs to write records" % (t_end - t_start))

