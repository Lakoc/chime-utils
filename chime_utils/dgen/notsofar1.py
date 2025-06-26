import glob
import json
import logging
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import soundfile as sf

from chime_utils.dgen.azure_storage import download_meeting_subset
from chime_utils.dgen.utils import get_mappings, symlink
from chime_utils.text_norm import get_txt_norm

logging.basicConfig(
    format=(
        "%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d]" " %(message)s"
    ),
    datefmt="%Y-%m-%d:%H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

NOTSOFAR1_FS = 16000


_check_version_exists_cache = None


def check_version_exists(version):
    global _check_version_exists_cache
    if _check_version_exists_cache is None:
        cmd = (
            "az storage blob list "
            "--container-name benchmark-datasets "
            "--account-name notsofarsa "
            '--prefix "" '
            '--delimiter "MTG" '
            '--query "[].name"'
        )
        _check_version_exists_cache = subprocess.check_output(cmd, shell=True).decode(
            "utf-8"
        )

    if version in _check_version_exists_cache:
        return True
    else:
        raise RuntimeError(
            f"Version {version} does not exist (anymore) in NOTSOFAR1 dataset !\n"
            f"Available: (pattern: <subset_name>/<version>/...)"
            f" {_check_version_exists_cache}"
        )


def download_notsofar1(download_dir, subset_name):
    if subset_name == "dev":
        subset_name = "dev_set"
        version = "240825.1_dev1"
    elif subset_name == "train_legacy":
        subset_name = "train_set"
        version = "240501.1_train"
    elif subset_name == "train":
        subset_name = "train_set"
        version = "240825.1_train"
    elif subset_name == "eval":
        subset_name = "eval_set"
        version = "240629.1_eval_small_with_GT"
    else:
        raise RuntimeError("Evaluation data has not yet been released !")
    try:
        dev_meetings_dir = download_meeting_subset(
            subset_name=subset_name, version=version, destination_dir=str(download_dir)
        )
    except FileNotFoundError:
        check_version_exists(version)  # will raise a better exception message
        raise

    if dev_meetings_dir is None:
        logger.error(f"Failed to download {subset_name} for NOTSOFAR1 dataset")

    return os.path.join(download_dir, subset_name, version, "MTG")


def normalize_notsofar1_annotation(
    transcriptions, session_name, txt_normalization, spk_map
):
    # Sam: this is FUGLY but works
    output = []
    output_normalized = []
    for entry in transcriptions:
        c_copy = deepcopy(entry)
        c_copy["session_id"] = session_name
        c_copy["start_time"] = str(entry["start_time"])
        c_copy["end_time"] = str(entry["end_time"])
        c_copy["speaker"] = spk_map[entry["speaker_id"]]
        del c_copy["speaker_id"]
        c_copy["word_timing"] = [
            [x[0], str(x[1]), str(x[2])] for x in entry["word_timing"]
        ]
        c_copy["words"] = deepcopy(entry["text"])
        del c_copy["text"]
        output.append(deepcopy(c_copy))  # need to copy again here

        c_copy["words"] = txt_normalization(c_copy["words"])
        if len(c_copy["words"]) == 0:
            continue

        del c_copy["word_timing"]
        del c_copy["ct_wav_file_name"]
        output_normalized.append(c_copy)

    output = sorted(output, key=lambda x: float(x["start_time"]))
    output_normalized = sorted(output_normalized, key=lambda x: float(x["start_time"]))

    return output, output_normalized


def convert2chime(
    c_split,
    audio_dir,
    session_name,
    spk_map,
    txt_normalization,
    output_root,
    is_sc=False,
):
    output_audio_f = os.path.join(output_root, "audio", c_split)
    os.makedirs(output_audio_f, exist_ok=True)

    output_devices_info = os.path.join(output_root, "devices", c_split)
    os.makedirs(output_devices_info, exist_ok=True)

    if c_split in ["train", "train_sc", "dev", "dev_sc", "eval", "eval_sc"]:
        # dump transcriptions
        output_txt_f = os.path.join(output_root, "transcriptions", c_split)
        os.makedirs(output_txt_f, exist_ok=True)

        output_txt_f_norm = os.path.join(output_root, "transcriptions_scoring", c_split)
        os.makedirs(output_txt_f_norm, exist_ok=True)

        # load device info here we need it to get the speaker mapping

        with open(
            os.path.join(Path(audio_dir).parent, "gt_meeting_metadata.json"), "r"
        ) as f:
            metadata = json.load(f)

        device2spk = {
            e: spk_map[k] for k, e in metadata["ParticipantAliasToCtDevice"].items()
        }

    far_field_audio = glob.glob(os.path.join(audio_dir, "*.wav"))
    devices_info = {}
    for elem in far_field_audio:
        # create symbolic link
        filename = Path(elem).stem
        channel_num = int(filename.strip("ch")) + 1
        device_name = f"{session_name}_U01.CH{channel_num}"
        tgt_name = os.path.join(
            output_audio_f,
            "{}.wav".format(device_name),
        )
        symlink(elem, tgt_name)
        dev_type = "circular_array" if not is_sc else "array_after_acoustic_frontend"
        d_type = {
            "is_close_talk": False,
            "speaker": None,
            "channel": channel_num,
            "tot_channels": 1,
            "device_type": dev_type,
        }
        devices_info[device_name] = d_type

    if c_split not in ["train", "train_sc", "dev", "dev_sc", "eval", "eval_sc"]:
        devices_info = dict(sorted(devices_info.items(), key=lambda x: x[0]))
        with open(os.path.join(output_devices_info, f"{session_name}.json"), "w") as f:
            json.dump(devices_info, f, indent=4)
        return  # no close talk and transcriptions

    # generate all other infos
    close_talk_audio = glob.glob(
        os.path.join(Path(audio_dir).parent, "close_talk", "*.wav")
    )
    for elem in close_talk_audio:
        # if c_split in ["eval", "dev"]:
        #    break
        filename = Path(elem).stem
        tgt_name = os.path.join(
            output_audio_f,
            "{}_{}.wav".format(session_name, device2spk[filename]),
        )
        symlink(elem, tgt_name)
        d_type = {
            "is_close_talk": True,
            "speaker": device2spk[filename],
            "channel": 1,
            "tot_channels": 1,
            "device_type": "close_talk_lapel",
        }
        devices_info[f"{session_name}_{device2spk[filename]}"] = d_type

    # load now transcription JSON and make some modifications
    with open(os.path.join(Path(audio_dir).parent, "gt_transcription.json"), "r") as f:
        transcriptions = json.load(f)

    output, output_normalized = normalize_notsofar1_annotation(
        transcriptions, session_name, txt_normalization, spk_map
    )

    with open(os.path.join(output_txt_f, f"{session_name}.json"), "w") as f:
        json.dump(output, f, indent=4)

    with open(os.path.join(output_txt_f_norm, f"{session_name}.json"), "w") as f:
        json.dump(output_normalized, f, indent=4)

    devices_info = dict(sorted(devices_info.items(), key=lambda x: x[0]))

    with open(os.path.join(output_devices_info, f"{session_name}.json"), "w") as f:
        json.dump(devices_info, f, indent=4)


def gen_notsofar1(
    output_dir, corpus_dir, download=False, dset_part="dev", challenge="chime8"
):
    corpus_dir = Path(corpus_dir).resolve()  # allow for relative path
    mapping = get_mappings(challenge)
    spk_map = mapping["spk_map"]["notsofar1"]
    sess_map = mapping["sessions_map"]["notsofar1"]
    text_normalization = get_txt_norm(challenge)
    corpus_dir = os.path.join(corpus_dir, dset_part)
    if download:
        corpus_dir = download_notsofar1(corpus_dir, subset_name=dset_part)

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # this is available even when no ground truth
    device_jsons = glob.glob(
        os.path.join(corpus_dir, "**/devices.json"), recursive=True
    )

    if len(device_jsons) == 0:
        logger.error(
            f"{corpus_dir} does not seem to contain NOTSOFAR1 meetings and metadata,"
            f" something is wrong ! "
            f"Maybe you wanted to --download the corpus and forgot to set the flag ?\n"
            f"Argument CORPUS_DIR should either be the target downloading folder "
            f"or contain meetings subfolders e.g: MTG_30860/ MTG_30862/ MTG_30865."
        )

    device_jsons = sorted(device_jsons, key=lambda x: Path(x).parent.stem)

    uem_file = os.path.join(output_dir, "uem", dset_part, "all.uem")
    Path(uem_file).parent.mkdir(parents=True, exist_ok=True)
    uem_data = []

    for device_j in device_jsons:
        orig_sess_name = Path(device_j).parent.stem

        with open(device_j, "r") as f:
            devices_info = json.load(f)

        mc_devices = [
            x
            for x in devices_info
            if x["is_close_talk"] is False and x["is_mc"] is True
        ]

        for mc_device in mc_devices:
            device_folder = os.path.join(
                Path(device_j).parent, f"mc_{mc_device['device_name']}"
            )
            if not os.path.exists(device_folder):
                logging.warning(
                    f"Can't locate any directory for "
                    f"{mc_device['device_name']} in {orig_sess_name} folder."
                )
                continue
            device_name = mc_device["device_name"]
            sess_name = sess_map[f"{orig_sess_name}_{device_name}_mc"]
            convert2chime(
                dset_part,
                device_folder,
                sess_name,
                spk_map,
                text_normalization,
                output_dir,
            )

            # use close talk 0 to get UEM
            ct_audio = glob.glob(
                os.path.join(Path(device_j).parent, "mc_plaza_0", "*.wav")
            )[0]
            info = sf.SoundFile(ct_audio)
            c_duration = info.frames / NOTSOFAR1_FS
            uem_data.append(
                "{} 1 {} {}\n".format(
                    sess_name,
                    "{:.3f}".format(float(0.0)),
                    "{:.3f}".format(float(c_duration)),
                )
            )

    with open(uem_file, "w") as f:
        f.writelines(uem_data)

    # also prep sc data
    uem_file_sc = os.path.join(output_dir, "uem", f"{dset_part}_sc", "all.uem")
    Path(uem_file_sc).parent.mkdir(parents=True, exist_ok=True)
    uem_data_sc = []
    for device_j in device_jsons:
        orig_sess_name = Path(device_j).parent.stem

        with open(device_j, "r") as f:
            devices_info = json.load(f)

        # also dump single channel as train_sc
        sc_devices = [
            x
            for x in devices_info
            if x["is_close_talk"] is False and x["is_mc"] is False
        ]

        for sc_device in sc_devices:
            device_folder = os.path.join(
                Path(device_j).parent, f"sc_{sc_device['device_name']}"
            )
            if not os.path.exists(device_folder):
                logging.warning(
                    f"Can't locate any directory for "
                    f"{sc_device['device_name']} in {orig_sess_name} folder."
                )
                continue
            device_name = sc_device["device_name"]
            sess_name = sess_map[f"{orig_sess_name}_{device_name}_sc"]

            convert2chime(
                f"{dset_part}_sc",
                device_folder,
                sess_name,
                spk_map,
                text_normalization,
                output_dir,
                is_sc=True,
            )

            ct_audio = glob.glob(
                os.path.join(Path(device_j).parent, "mc_plaza_0", "*.wav")
            )[0]
            info = sf.SoundFile(ct_audio)
            c_duration = info.frames / NOTSOFAR1_FS
            uem_data_sc.append(
                "{} 1 {} {}\n".format(
                    sess_name,
                    "{:.3f}".format(float(0.0)),
                    "{:.3f}".format(float(c_duration)),
                )
            )

    with open(uem_file_sc, "w") as f:
        f.writelines(uem_data_sc)
