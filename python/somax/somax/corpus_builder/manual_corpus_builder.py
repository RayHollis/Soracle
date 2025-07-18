import logging
import multiprocessing
import re
from typing import Dict, Type, List, Optional, Any, Tuple

import librosa
import numpy as np
from audioread import NoBackendError

from somax.corpus_builder.corpus_builder import CorpusBuilder
from somax.corpus_builder.manual_text_formats import TextFormat, ParsingError
from somax.corpus_builder.metadata import AudioMetadata
from somax.corpus_builder.spectrogram import Spectrogram
from somax.features import YinDiscretePitch, TotalEnergyDb
from somax.features.feature import CorpusFeature
from somax.runtime.corpus import Corpus, AudioCorpus
from somax.runtime.corpus_event import AudioCorpusEvent
from somax.runtime.exceptions import FeatureError, ParameterError
from somax.runtime.osc_log_forwarder import OscLogForwarder
from somax.runtime.send_protocol import PlayerSendProtocol, ServerSendProtocol
from somax.runtime.target import SimpleOscTarget
from somax.scheduler.scheduling_mode import AbsoluteScheduling


class Descriptors:
    @staticmethod
    def _descriptors() -> Dict[str, Type[CorpusFeature]]:
        # raise NotImplementedError("Not implemented yet")
        return {"pitch": YinDiscretePitch,
                "energy": TotalEnergyDb}
        #         "chroma": OnsetChroma}

    @staticmethod
    def keywords() -> List[str]:
        return list(Descriptors._descriptors().keys())

    @staticmethod
    def from_keyword(keyword: str) -> Type[CorpusFeature]:
        return Descriptors._descriptors()[keyword]

    @staticmethod
    def get_keyword(corpus_feature_type: Type[CorpusFeature]) -> str:
        return {v: k for k, v in Descriptors._descriptors().items()}[corpus_feature_type]


class ThreadedManualCorpusBuilder(multiprocessing.Process):
    def __init__(self,
                 audio_file_path: str,
                 analysis_file_path: str,
                 output_folder: str,
                 analysis_format: Type[TextFormat],
                 ip: str,
                 send_port: int,
                 osc_address: str,
                 corpus_name: Optional[str] = None,
                 pre_analysed_descriptors: Optional[List[Type[CorpusFeature]]] = None,
                 use_tempo_annotations: bool = False,
                 segmentation_offset_ms: int = 0,
                 ignore_invalid_lines: bool = False,
                 overwrite: bool = False,
                 copy_resources: bool = False,
                 builder_address: str = "",
                 log_level: int = logging.INFO, ):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.audio_file_path: str = audio_file_path
        self.analysis_file_path: str = analysis_file_path
        self.output_folder = output_folder
        self.corpus_name: str = corpus_name
        self.analysis_format: Type[TextFormat] = analysis_format
        self.pre_analysed_descriptors: Optional[List[Type[CorpusFeature]]] = pre_analysed_descriptors
        self.use_tempo_annotations: bool = use_tempo_annotations
        self.segmentation_offset_ms: int = segmentation_offset_ms
        self.ignore_invalid_lines: bool = ignore_invalid_lines
        self.overwrite: bool = overwrite
        self.copy_resources: bool = copy_resources
        self.builder_address: str = builder_address
        self.log_level: int = log_level
        self.target = SimpleOscTarget(address=osc_address, port=send_port, ip=ip)

    def run(self) -> None:
        logging.basicConfig(level=self.log_level, format="[%(levelname)s]: %(message)s")
        self.logger.addHandler(OscLogForwarder(self.target))
        self.target.send(ServerSendProtocol.MANUAL_CORPUSBUILDER_STATUS, ["init", self.builder_address])
        try:
            self.logger.info(f"Building manually annotated corpus '{self.corpus_name}'")
            corpus: Corpus = ManualCorpusBuilder().build(audio_file_path=self.audio_file_path,
                                                         analysis_file_path=self.analysis_file_path,
                                                         corpus_name=self.corpus_name,
                                                         analysis_format=self.analysis_format,
                                                         pre_analysed_descriptors=self.pre_analysed_descriptors,
                                                         use_tempo_annotations=self.use_tempo_annotations,
                                                         segmentation_offset_ms=self.segmentation_offset_ms,
                                                         ignore_invalid_lines=self.ignore_invalid_lines)

        except NotImplementedError as e:
            self.logger.error(f"{str(e)}. No corpus was built")
            self.target.send(ServerSendProtocol.MANUAL_CORPUSBUILDER_STATUS, ["failed", self.builder_address])
            return

        except RuntimeError as e:
            self.logger.error(f"{str(e)}. Could not parse annotation file")
            self.target.send(ServerSendProtocol.MANUAL_CORPUSBUILDER_STATUS, ["failed", self.builder_address])
            return

        except (ValueError,
                IOError,
                ParameterError,
                librosa.util.exceptions.ParameterError,
                FileNotFoundError) as e:
            self.logger.error(f"{str(e)}. No corpus was built")
            self.target.send(ServerSendProtocol.MANUAL_CORPUSBUILDER_STATUS, ["failed", self.builder_address])
            return

        except NoBackendError:
            self.logger.error(f"The file format of the provided file is not supported.")
            self.target.send(ServerSendProtocol.MANUAL_CORPUSBUILDER_STATUS, ["failed", self.builder_address])
            return

        try:
            self.logger.info(f"Exporting corpus to folder '{self.output_folder}'")
            output_filepath: str = corpus.export(self.output_folder,
                                                 overwrite=self.overwrite,
                                                 copy_resources=self.copy_resources)

            self.target.send(ServerSendProtocol.MANUAL_CORPUSBUILDER_STATUS, ["success", self.builder_address])
            self.logger.info(f"Corpus was successfully written to file '{output_filepath}'.")

        except (IOError, AttributeError, KeyError) as e:
            self.logger.error(f"{str(e)} Export of corpus failed.")
            self.target.send(ServerSendProtocol.MANUAL_CORPUSBUILDER_STATUS, ["failed", self.builder_address])
            return


class ManualCorpusBuilder:
    HOP_LENGTH = 512

    def __init__(self, verbose: bool = False):
        self.logger = logging.getLogger(__name__)
        if verbose:
            self.logger.setLevel(logging.DEBUG)

    def build(self,
              audio_file_path: str,
              analysis_file_path: str,
              corpus_name: str,
              analysis_format: Type[TextFormat],
              pre_analysed_descriptors: Optional[List[Type[CorpusFeature]]] = None,
              use_tempo_annotations: bool = False,
              segmentation_offset_ms: int = 0,
              ignore_invalid_lines: bool = False) -> Corpus:
        """ raises: RuntimeError if invalid line encountered and `ignore_invalid_line` is False
                    NotImplementedError for certain features
        """

        pre_analysed_descriptors = pre_analysed_descriptors if pre_analysed_descriptors is not None else []

        onsets: List[float]
        offsets: List[Optional[float]]
        onsets, offsets = analysis_format.parse_file(analysis_file_path=analysis_file_path,
                                                     use_tempo_annotations=use_tempo_annotations,
                                                     pre_analysed_descriptors=pre_analysed_descriptors,
                                                     ignore_invalid_lines=ignore_invalid_lines)

        if len(onsets) == 0:
            raise RuntimeError("Annotation file did not contain any valid lines")

        x, sr = librosa.load(audio_file_path, sr=None, mono=False)
        x_mono = CorpusBuilder._parse_channels(x)
        file_duration: float = x_mono.size / sr

        onset_array: np.ndarray
        duration_array: np.ndarray
        onset_array, duration_array = self.parse_onsets_and_durations(onsets, offsets, eof=file_duration)

        # adjust in time
        onset_array = np.maximum(0.0, onset_array + segmentation_offset_ms * 0.001)

        events: List[AudioCorpusEvent] = [AudioCorpusEvent(state_index=i,
                                                           absolute_onset=onset,
                                                           absolute_duration=duration)
                                          for i, (onset, duration) in enumerate(zip(onset_array, duration_array))]

        stft: Spectrogram = Spectrogram.from_audio(x_mono, sample_rate=sr, hop_length=self.HOP_LENGTH)
        metadata: AudioMetadata = AudioMetadata(filepath=audio_file_path,
                                                content_type=AbsoluteScheduling(),
                                                raw_data=x,
                                                foreground_data=x_mono,
                                                background_data=x_mono,
                                                sr=sr,
                                                hop_length=self.HOP_LENGTH,
                                                stft=stft,
                                                estimated_initial_bpm=120.0,
                                                beat_tightness=1.0)

        used_features = []
        for _, feature in CorpusFeature.all_corpus_features():
            try:
                if feature not in used_features:
                    feature.analyze(events, metadata)
                used_features.append(feature)
            except FeatureError:
                self.logger.debug(f"ignoring feature {feature.__name__}")

        corpus: AudioCorpus = AudioCorpus(events=events,
                                          name=corpus_name,
                                          scheduling_mode=metadata.content_type,
                                          feature_types=used_features,
                                          build_parameters={},
                                          sr=sr,
                                          filepath=audio_file_path,
                                          file_duration=metadata.duration,
                                          file_num_channels=metadata.channels)

        return corpus

    @staticmethod
    def parse_onsets_and_durations(onsets: List[float],
                                   offsets: List[Optional[float]],
                                   eof: float) -> Tuple[np.ndarray, np.ndarray]:
        if len(onsets) != len(offsets):
            raise RuntimeError("Onset and offset arrays are of different lengths")

        onset_array = np.array(onsets, dtype=float)
        has_offset: np.ndarray = np.array([offset is not None for offset in offsets], dtype=bool)

        # All offsets provided
        if np.all(has_offset):
            duration_array = np.array(offsets) - onsets

        # Some offsets provided
        elif np.any(has_offset):
            duration_array = np.zeros_like(onset_array, dtype=float)
            for i in range(len(onset_array) - 1):
                if has_offset[i]:
                    duration_array[i] = offsets[i] - onset_array[i]
                else:
                    duration_array[i] = onset_array[i + 1] - onset_array[i]

            # Last offset
            if has_offset[-1]:
                duration_array[-1] = eof - offsets[-1]
            else:
                duration_array[-1] = eof - onset_array[-1]

        # No offsets provided
        else:
            duration_array = np.block([np.diff(onsets), eof - onsets[-1]])

        return onset_array, duration_array
