import math
from typing import Collection, Dict, List, Tuple, Union

import numpy as np
import torch
from typeguard import typechecked

from espnet.nets.pytorch_backend.nets_utils import pad_list
import soundfile
from espnet2.train.preprocessor import detect_non_silence
import random


class CommonCollateFn:
    """Functor class of common_collate_fn()"""

    @typechecked
    def __init__(
        self,
        float_pad_value: Union[float, int] = 0.0,
        int_pad_value: int = -32768,
        not_sequence: Collection[str] = (),
    ):
        self.float_pad_value = float_pad_value
        self.int_pad_value = int_pad_value
        self.not_sequence = set(not_sequence)

    def __repr__(self):
        return (
            f"{self.__class__}(float_pad_value={self.float_pad_value}, "
            f"int_pad_value={self.float_pad_value})"
        )

    def __call__(
        self, data: Collection[Tuple[str, Dict[str, np.ndarray]]]
    ) -> Tuple[List[str], Dict[str, torch.Tensor]]:
        return common_collate_fn(
            data,
            float_pad_value=self.float_pad_value,
            int_pad_value=self.int_pad_value,
            not_sequence=self.not_sequence,
        )


class HuBERTCollateFn(CommonCollateFn):
    """Functor class of common_collate_fn()"""

    @typechecked
    def __init__(
        self,
        float_pad_value: Union[float, int] = 0.0,
        int_pad_value: int = -32768,
        label_downsampling: int = 1,
        pad: bool = False,
        rand_crop: bool = True,
        crop_audio: bool = True,
        not_sequence: Collection[str] = (),
        window_size: float = 25,
        window_shift: float = 20,
        sample_rate: float = 16,
        noise_scp: str = "data/noise/wav.scp",
        noise_apply_prob: float = 1.0,
        noise_db_range: str = "-5_20",
        dynamic_mixing_gain_db: float = 5.0,
        dynamic_mixing_prob: float = 0.1,
        mix_speech: bool = False,
        train: bool = True,
    ):
        super().__init__(
            float_pad_value=float_pad_value,
            int_pad_value=int_pad_value,
            not_sequence=not_sequence,
        )
        self.float_pad_value = float_pad_value
        self.int_pad_value = int_pad_value
        self.label_downsampling = label_downsampling
        self.pad = pad
        self.rand_crop = rand_crop
        self.crop_audio = crop_audio
        self.not_sequence = set(not_sequence)
        self.window_size = window_size
        self.window_shift = window_shift
        self.sample_rate = sample_rate
        self.train = train
        self.mix_speech = mix_speech
        self.dynamic_mixing_prob = dynamic_mixing_prob
        self.noise_apply_prob = noise_apply_prob
        self.dynamic_mixing_gain_db = dynamic_mixing_gain_db

        if train and mix_speech and noise_scp is not None:
            print("reloading noises", flush=True)
            self.noises = {}
            self.noise_paths = []
            with open(noise_scp, "r", encoding="utf-8") as f:
                for line in f:
                    sps = line.strip().split(None, 1)
                    if len(sps) == 1:
                        noise_path = sps[0]
                    else:
                        noise_path = sps[1]
                    self.noise_paths.append(noise_path)
                    # with soundfile.SoundFile(noise_path) as f:
                    #     self.noises.append(f.read(dtype=np.float32, always_2d=False).squeeze())
            sps = noise_db_range.split("_")
            if len(sps) == 1:
                self.noise_db_low = self.noise_db_high = float(sps[0])
            elif len(sps) == 2:
                self.noise_db_low, self.noise_db_high = float(sps[0]), float(sps[1])
            else:
                raise ValueError(
                    "Format error: '{noise_db_range}' e.g. -3_4 -> [-3db,4db]"
                )
        else:
            self.noises = None

    def _read_noise_audio_(self):
        noise_path = np.random.choice(self.noise_paths)
        noise = None
        if noise_path is not None:
            if noise_path in self.noises:
                noise = self.noises[noise_path]
            else:
                with soundfile.SoundFile(noise_path) as f:
                    noise = f.read(dtype=np.float32, always_2d=False)
                self.noises[noise_path] = noise
        return noise

    def _add_noise_wavlm(self, data, speech, speech_id):
        power = (speech[detect_non_silence(speech)] ** 2).mean()
        if self.dynamic_mixing_prob >= np.random.random() or len(data) == 1:
            noise = self._read_noise_audio_().squeeze()
            # noise = np.random.choice(self.noises)
            noise_db = np.random.uniform(self.noise_db_low, self.noise_db_high)
        else:
            noise = random.choice(data)
            while noise[0] == speech_id:
                noise = random.choice(data)
            noise = noise[1]["speech"]
            speech_length = speech.shape[0]
            noise_db = np.random.uniform(
                -self.dynamic_mixing_gain_db, self.dynamic_mixing_gain_db
            )

        length = min(np.random.randint(1, len(speech) // 2 + 1), len(noise))
        speech_start = np.random.randint(0, len(speech) - length + 1)
        noise_start = np.random.randint(0, len(noise) - length + 1)

        noise_power = (noise**2).mean()
        scale = (
            10 ** (-noise_db / 20) * np.sqrt(power) / np.sqrt(max(noise_power, 1e-10))
        )
        noise = noise * scale
        speech[speech_start : speech_start + length] += noise[
            noise_start : noise_start + length
        ]

        return speech

    def __repr__(self):
        return (
            f"{self.__class__}(float_pad_value={self.float_pad_value}, "
            f"int_pad_value={self.float_pad_value}, "
            f"label_downsampling={self.label_downsampling}, "
            f"pad_value={self.pad}, rand_crop={self.rand_crop}) "
        )

    def __call__(
        self, data: Collection[Tuple[str, Dict[str, np.ndarray]]]
    ) -> Tuple[List[str], Dict[str, torch.Tensor]]:
        assert "speech" in data[0][1]
        assert "text" in data[0][1]
        if self.pad:
            num_frames = max([sample["speech"].shape[0] for uid, sample in data])
        else:
            num_frames = min([sample["speech"].shape[0] for uid, sample in data])

        new_data = []
        if self.train or self.label_downsampling > 1:
            for uid, sample in data:
                waveform, label = sample["speech"], sample["text"]
                assert waveform.ndim == 1
                length = waveform.size
                # The MFCC feature is 10ms per frame, while the HuBERT's transformer output
                # is 20ms per frame. Downsample the KMeans label if it's generated by MFCC
                # features.
                if (
                    self.train
                    and self.mix_speech
                    and self.noise_apply_prob >= np.random.random()
                ):
                    waveform = self._add_noise_wavlm(data, waveform, uid)
                if self.label_downsampling > 1:
                    label = label[:: self.label_downsampling]
                if self.train and self.crop_audio:
                    waveform, label, length = _crop_audio_label(
                        waveform,
                        label,
                        length,
                        num_frames,
                        self.rand_crop,
                        self.window_size,
                        self.window_shift,
                        self.sample_rate,
                    )
                new_data.append((uid, dict(speech=waveform, text=label)))
        else:
            new_data = data

        return common_collate_fn(
            new_data,
            float_pad_value=self.float_pad_value,
            int_pad_value=self.int_pad_value,
            not_sequence=self.not_sequence,
        )


def _crop_audio_label(
    waveform: torch.Tensor,
    label: torch.Tensor,
    length: torch.Tensor,
    num_frames: int,
    rand_crop: bool,
    window_size: int = 25,
    window_shift: int = 20,
    sample_rate: int = 16,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Collate the audio and label at the same time.

    Args:
        waveform (Tensor): The waveform Tensor with dimensions `(time)`.
        label (Tensor): The label Tensor with dimensions `(seq)`.
        length (Tensor): The length Tensor with dimension `(1,)`.
        num_frames (int): The final length of the waveform.
        rand_crop (bool): if ``rand_crop`` is True, the starting index of the
            waveform and label is random if the length is longer than the minimum
            length in the mini-batch.
        window_size (int): reception field of conv feature extractor (in ms).
            In default, calculated by [400 (samples) / 16 (sample_rate)].
        window_shift (int): the stride of conv feature extractor (in ms).
            In default, calculated by [320 (samples) / 16 (sample_rate)].
        sample_rate (int): number of samples in audio signal per millisecond.

    Returns:
        (Tuple(Tensor, Tensor, Tensor)): Returns the Tensors for the waveform,
            label, and the waveform length.

    """

    frame_offset = 0
    if waveform.size > num_frames and rand_crop:
        diff = waveform.size - num_frames
        frame_offset = torch.randint(diff, size=(1,))
    elif waveform.size < num_frames:
        num_frames = waveform.size
    label_offset = max(
        math.floor(
            (frame_offset - window_size * sample_rate) / (window_shift * sample_rate)
        )
        + 1,
        0,
    )
    num_label = (
        math.floor(
            (num_frames - window_size * sample_rate) / (window_shift * sample_rate)
        )
        + 1
    )
    waveform = waveform[frame_offset : frame_offset + num_frames]
    label = label[label_offset : label_offset + num_label]
    length = num_frames

    return waveform, label, length


@typechecked
def common_collate_fn(
    data: Collection[Tuple[str, Dict[str, np.ndarray]]],
    float_pad_value: Union[float, int] = 0.0,
    int_pad_value: int = -32768,
    not_sequence: Collection[str] = (),
) -> Tuple[List[str], Dict[str, torch.Tensor]]:
    """Concatenate ndarray-list to an array and convert to torch.Tensor.

    Examples:
        >>> from espnet2.samplers.constant_batch_sampler import ConstantBatchSampler,
        >>> import espnet2.tasks.abs_task
        >>> from espnet2.train.dataset import ESPnetDataset
        >>> sampler = ConstantBatchSampler(...)
        >>> dataset = ESPnetDataset(...)
        >>> keys = next(iter(sampler)
        >>> batch = [dataset[key] for key in keys]
        >>> batch = common_collate_fn(batch)
        >>> model(**batch)

        Note that the dict-keys of batch are propagated from
        that of the dataset as they are.

    """
    uttids = [u for u, _ in data]
    data = [d for _, d in data]

    assert all(set(data[0]) == set(d) for d in data), "dict-keys mismatching"
    assert all(
        not k.endswith("_lengths") for k in data[0]
    ), f"*_lengths is reserved: {list(data[0])}"

    output = {}
    for key in data[0]:
        # NOTE(kamo):
        # Each models, which accepts these values finally, are responsible
        # to repaint the pad_value to the desired value for each tasks.
        if data[0][key].dtype.kind == "i":
            pad_value = int_pad_value
        else:
            pad_value = float_pad_value

        array_list = [d[key] for d in data]

        # Assume the first axis is length:
        # tensor_list: Batch x (Length, ...)
        tensor_list = [torch.from_numpy(a) for a in array_list]
        # tensor: (Batch, Length, ...)
        tensor = pad_list(tensor_list, pad_value)
        output[key] = tensor

        # lens: (Batch,)
        if key not in not_sequence:
            lens = torch.tensor([d[key].shape[0] for d in data], dtype=torch.long)
            output[key + "_lengths"] = lens

    output = (uttids, output)
    return output
