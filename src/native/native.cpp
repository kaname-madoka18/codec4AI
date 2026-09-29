#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <deque>
#include <fcntl.h>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>
#include <vector>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
#include <libavutil/dict.h>
#include <libavutil/error.h>
#include <libavutil/imgutils.h>
#include <libavutil/mathematics.h>
#include <libavutil/mem.h>
#include <libavutil/opt.h>
#include <libswscale/swscale.h>
}

namespace py = pybind11;

namespace {

std::string ff_error(int error) {
    char buffer[AV_ERROR_MAX_STRING_SIZE] = {};
    av_strerror(error, buffer, sizeof(buffer));
    return std::string(buffer);
}

void check_ff(int result, const char* operation, const char* prefix = "decode:") {
    if (result < 0) {
        throw std::runtime_error(
            std::string(prefix) + " " + operation + ": " + ff_error(result)
        );
    }
}

class FileDescriptor {
public:
    FileDescriptor() = default;
    explicit FileDescriptor(int value) : value_(value) {}
    ~FileDescriptor() {
        if (value_ >= 0) {
            close(value_);
        }
    }
    FileDescriptor(const FileDescriptor&) = delete;
    FileDescriptor& operator=(const FileDescriptor&) = delete;
    FileDescriptor(FileDescriptor&& other) noexcept : value_(other.value_) {
        other.value_ = -1;
    }
    FileDescriptor& operator=(FileDescriptor&& other) noexcept {
        if (this != &other) {
            if (value_ >= 0) {
                close(value_);
            }
            value_ = other.value_;
            other.value_ = -1;
        }
        return *this;
    }
    int get() const { return value_; }

private:
    int value_ = -1;
};

void write_all(int fd, const uint8_t* data, size_t size) {
    size_t written = 0;
    while (written < size) {
        const ssize_t result = write(fd, data + written, size - written);
        if (result < 0 && errno == EINTR) {
            continue;
        }
        if (result <= 0) {
            throw std::runtime_error("invalid mp4: failed to stage bytes payload");
        }
        written += static_cast<size_t>(result);
    }
}

class InputSource {
public:
    explicit InputSource(const py::object& source) {
        if (py::isinstance<py::str>(source)) {
            const std::string path = py::cast<std::string>(source);
            const int descriptor = open(path.c_str(), O_RDONLY | O_CLOEXEC);
            if (descriptor < 0) {
                throw std::runtime_error(
                    "invalid mp4: cannot open input path: "
                    + std::string(std::strerror(errno))
                );
            }
            fd_ = FileDescriptor(descriptor);
        } else if (py::isinstance<py::bytes>(source)) {
            char* data = nullptr;
            Py_ssize_t length = 0;
            if (PyBytes_AsStringAndSize(source.ptr(), &data, &length) != 0) {
                throw py::error_already_set();
            }
            if (length <= 0) {
                throw std::runtime_error("invalid mp4: empty bytes payload");
            }
            const int descriptor = memfd_create("video_loader_mp4", MFD_CLOEXEC);
            if (descriptor < 0) {
                throw std::runtime_error(
                    "invalid mp4: memfd_create failed: "
                    + std::string(std::strerror(errno))
                );
            }
            fd_ = FileDescriptor(descriptor);
            try {
                write_all(
                    fd_.get(),
                    reinterpret_cast<const uint8_t*>(data),
                    static_cast<size_t>(length)
                );
            } catch (...) {
                throw;
            }
            if (lseek(fd_.get(), 0, SEEK_SET) < 0) {
                throw std::runtime_error("invalid mp4: failed to rewind bytes payload");
            }
        } else {
            throw std::runtime_error("invalid mp4: native source must be str or bytes");
        }

        struct stat status {};
        if (fstat(fd_.get(), &status) != 0 || status.st_size <= 0) {
            throw std::runtime_error("invalid mp4: input is empty or not a regular payload");
        }
        size_ = static_cast<int64_t>(status.st_size);
        path_ = "/proc/self/fd/" + std::to_string(fd_.get());
    }

    int fd() const { return fd_.get(); }
    int64_t size() const { return size_; }
    const std::string& path() const { return path_; }

private:
    FileDescriptor fd_;
    int64_t size_ = 0;
    std::string path_;
};

struct FormatCloser {
    void operator()(AVFormatContext* value) const {
        if (value != nullptr) {
            avformat_close_input(&value);
        }
    }
};

struct PacketCloser {
    void operator()(AVPacket* value) const {
        if (value != nullptr) {
            av_packet_free(&value);
        }
    }
};

struct FrameCloser {
    void operator()(AVFrame* value) const {
        if (value != nullptr) {
            av_frame_free(&value);
        }
    }
};

struct CodecContextCloser {
    void operator()(AVCodecContext* value) const {
        if (value != nullptr) {
            avcodec_free_context(&value);
        }
    }
};

struct SwsCloser {
    void operator()(SwsContext* value) const {
        if (value != nullptr) {
            sws_freeContext(value);
        }
    }
};

struct AvBufferCloser {
    void operator()(uint8_t* value) const {
        av_free(value);
    }
};

struct Sample {
    int display_index = -1;
    int decode_index = -1;
    int64_t pts = AV_NOPTS_VALUE;
    int64_t dts = AV_NOPTS_VALUE;
    int64_t duration = 0;
    int64_t offset = -1;
    int size = 0;
    bool is_sync = false;
    bool is_reference = false;
    bool is_random_access = false;
};

struct MediaIndex {
    AVCodecID codec_id = AV_CODEC_ID_NONE;
    int video_stream = -1;
    int width = 0;
    int height = 0;
    AVRational time_base{0, 1};
    AVRational frame_rate{0, 1};
    int64_t duration = AV_NOPTS_VALUE;
    std::string codec_name;
    std::vector<Sample> packets;
    std::vector<int> display_to_decode;
    std::vector<std::vector<int>> direct_references;
    bool reference_graph_available = false;
    int reference_graph_segment_begin = -1;
    int reference_graph_segment_end = -1;
    int reference_graph_packets = 0;
    int reference_graph_attempts = 0;

    const Sample& display(int index) const {
        return packets.at(
            static_cast<size_t>(display_to_decode.at(static_cast<size_t>(index)))
        );
    }
};

// Private ABI shared with the FFmpeg patch applied by
// tools/build_self_contained_wheel.sh.  Stock FFmpeg ignores AVCodecContext's
// opaque pointer, which lets source builds fail closed and use the conservative
// decode fallback without linking against a private FFmpeg symbol.
constexpr uint64_t REFERENCE_GRAPH_MAGIC = UINT64_C(0x564c524546475231);

using ReferenceGraphCallback = void (*)(
    void* opaque,
    int64_t current_sample,
    const int64_t* references,
    int reference_count,
    int displayed
);

struct ReferenceGraphBridge {
    uint64_t magic = REFERENCE_GRAPH_MAGIC;
    int64_t current_sample = -1;
    void* opaque = nullptr;
    ReferenceGraphCallback record = nullptr;
    int state_only = 0;
};

bool has_state_only_decoder(const AVCodecContext* context) {
    return context != nullptr
        && context->priv_data != nullptr
        && av_opt_find(
               context->priv_data, "video_loader_reference_graph",
               "video_loader_private", 0, 0
           ) != nullptr;
}

struct ReferenceGraphCollector {
    explicit ReferenceGraphCollector(size_t sample_count)
        : references(sample_count), seen(sample_count, false),
          displayed(sample_count, false) {}

    std::vector<std::set<int>> references;
    std::vector<bool> seen;
    std::vector<bool> displayed;
    bool invalid = false;
};

void collect_reference_graph(
    void* opaque,
    int64_t current_sample,
    const int64_t* references,
    int reference_count,
    int displayed
) {
    auto* collector = static_cast<ReferenceGraphCollector*>(opaque);
    if (collector == nullptr
        || current_sample < 0
        || static_cast<size_t>(current_sample) >= collector->references.size()
        || reference_count < 0
        || (reference_count > 0 && references == nullptr)) {
        if (collector != nullptr) {
            collector->invalid = true;
        }
        return;
    }

    const size_t current = static_cast<size_t>(current_sample);
    collector->seen[current] = true;
    collector->displayed[current] =
        collector->displayed[current] || displayed != 0;
    for (int index = 0; index < reference_count; ++index) {
        const int64_t reference = references[index];
        if (reference == current_sample) {
            continue;
        }
        if (reference < 0
            || reference >= current_sample
            || static_cast<size_t>(reference) >= collector->references.size()) {
            collector->invalid = true;
            continue;
        }
        collector->references[current].insert(static_cast<int>(reference));
    }
}

struct NalProperties {
    bool is_reference = false;
    bool is_random_access = false;
    bool saw_vcl = false;
};

void pread_exact(int fd, uint8_t* output, size_t size, int64_t offset) {
    size_t consumed = 0;
    while (consumed < size) {
        const ssize_t result = pread(
            fd,
            output + consumed,
            size - consumed,
            static_cast<off_t>(offset + static_cast<int64_t>(consumed))
        );
        if (result < 0 && errno == EINTR) {
            continue;
        }
        if (result <= 0) {
            throw std::runtime_error("invalid mp4: sample range cannot be read");
        }
        consumed += static_cast<size_t>(result);
    }
}

int nal_length_size(const AVCodecParameters* parameters) {
    if (parameters->extradata == nullptr || parameters->extradata_size <= 0) {
        throw std::runtime_error("unsupported: missing AVC/HEVC configuration record");
    }
    if (parameters->codec_id == AV_CODEC_ID_H264) {
        if (parameters->extradata_size < 5 || parameters->extradata[0] != 1) {
            throw std::runtime_error("unsupported: invalid H.264 avcC extradata");
        }
        return (parameters->extradata[4] & 0x03) + 1;
    }
    if (parameters->codec_id == AV_CODEC_ID_HEVC) {
        if (parameters->extradata_size < 22 || parameters->extradata[0] != 1) {
            throw std::runtime_error("unsupported: invalid HEVC hvcC extradata");
        }
        return (parameters->extradata[21] & 0x03) + 1;
    }
    throw std::runtime_error("unsupported: NAL parser received non-AVC/HEVC codec");
}

NalProperties sample_nal_properties(
    int fd,
    const Sample& sample,
    int length_size,
    AVCodecID codec_id
) {
    std::vector<uint8_t> payload(static_cast<size_t>(sample.size));
    pread_exact(fd, payload.data(), payload.size(), sample.offset);

    NalProperties properties;
    size_t cursor = 0;
    while (cursor < payload.size()) {
        if (cursor + static_cast<size_t>(length_size) > payload.size()) {
            throw std::runtime_error("invalid mp4: truncated NAL length field");
        }
        uint32_t nal_size = 0;
        for (int index = 0; index < length_size; ++index) {
            nal_size = (nal_size << 8) | payload[cursor + static_cast<size_t>(index)];
        }
        cursor += static_cast<size_t>(length_size);
        if (nal_size == 0 || cursor + nal_size > payload.size()) {
            throw std::runtime_error("invalid mp4: invalid NAL size in sample");
        }

        const uint8_t header = payload[cursor];
        if (codec_id == AV_CODEC_ID_H264) {
            const int nal_type = header & 0x1f;
            const int nal_ref_idc = (header >> 5) & 0x03;
            if (nal_type == 1 || nal_type == 5) {
                properties.saw_vcl = true;
                properties.is_reference =
                    properties.is_reference || nal_ref_idc != 0;
                properties.is_random_access =
                    properties.is_random_access || nal_type == 5;
            }
        } else {
            const int nal_type = (header >> 1) & 0x3f;
            if (nal_type <= 31) {
                properties.saw_vcl = true;
                properties.is_reference = properties.is_reference
                    || nal_type >= 16
                    || (nal_type & 1) != 0;
                properties.is_random_access = properties.is_random_access
                    || (nal_type >= 16 && nal_type <= 23);
            }
        }
        cursor += nal_size;
    }
    if (!properties.saw_vcl) {
        throw std::runtime_error("unsupported: compressed sample has no VCL NAL");
    }
    return properties;
}

std::unique_ptr<AVFormatContext, FormatCloser> open_format(
    const InputSource& source
) {
    AVFormatContext* raw = nullptr;
    const int opened = avformat_open_input(
        &raw,
        source.path().c_str(),
        nullptr,
        nullptr
    );
    if (opened < 0) {
        throw std::runtime_error(
            "invalid mp4: avformat_open_input: " + ff_error(opened)
        );
    }
    std::unique_ptr<AVFormatContext, FormatCloser> format(raw);
    check_ff(
        avformat_find_stream_info(format.get(), nullptr),
        "avformat_find_stream_info",
        "invalid mp4:"
    );
    const std::string format_name = format->iformat != nullptr
        && format->iformat->name != nullptr
        ? format->iformat->name
        : "";
    if (format_name.find("mov") == std::string::npos
        && format_name.find("mp4") == std::string::npos) {
        throw std::runtime_error("unsupported: input container is not MP4/ISO BMFF");
    }
    return format;
}

MediaIndex build_index(
    AVFormatContext* format,
    const InputSource& source
) {
    int video_tracks = 0;
    int video_stream = -1;
    for (unsigned int index = 0; index < format->nb_streams; ++index) {
        if (format->streams[index]->codecpar->codec_type == AVMEDIA_TYPE_VIDEO) {
            ++video_tracks;
            if (video_stream < 0) {
                video_stream = static_cast<int>(index);
            }
        }
    }
    if (video_stream < 0) {
        throw std::runtime_error("invalid mp4: no video track");
    }
    if (video_tracks != 1) {
        throw std::runtime_error("unsupported: MP4 must contain exactly one video track");
    }

    AVStream* stream = format->streams[video_stream];
    const AVCodecID codec_id = stream->codecpar->codec_id;
    if (codec_id != AV_CODEC_ID_H264
        && codec_id != AV_CODEC_ID_HEVC
        && codec_id != AV_CODEC_ID_AV1) {
        throw std::runtime_error(
            "unsupported: codec must be H.264, HEVC, or AV1"
        );
    }
    if (stream->codecpar->width <= 0 || stream->codecpar->height <= 0) {
        throw std::runtime_error("invalid mp4: video dimensions are missing");
    }

    const int length_size = codec_id == AV_CODEC_ID_AV1
        ? 0
        : nal_length_size(stream->codecpar);
    std::unique_ptr<AVPacket, PacketCloser> packet(av_packet_alloc());
    if (!packet) {
        throw std::runtime_error("decode: av_packet_alloc failed");
    }

    MediaIndex media;
    media.codec_id = codec_id;
    media.video_stream = video_stream;
    media.width = stream->codecpar->width;
    media.height = stream->codecpar->height;
    media.time_base = stream->time_base;
    media.frame_rate = av_guess_frame_rate(format, stream, nullptr);
    media.duration = stream->duration;
    media.codec_name = avcodec_get_name(codec_id);

    while (true) {
        const int result = av_read_frame(format, packet.get());
        if (result == AVERROR_EOF) {
            break;
        }
        check_ff(result, "av_read_frame", "invalid mp4:");
        if (packet->stream_index == video_stream) {
            Sample sample;
            sample.decode_index = static_cast<int>(media.packets.size());
            sample.pts = packet->pts;
            sample.dts = packet->dts;
            sample.duration = packet->duration;
            sample.offset = packet->pos;
            sample.size = packet->size;
            sample.is_sync = (packet->flags & AV_PKT_FLAG_KEY) != 0;

            const bool range_invalid = sample.offset < 0
                || sample.size <= 0
                || sample.offset > source.size()
                || static_cast<int64_t>(sample.size) > source.size() - sample.offset;
            if (sample.pts == AV_NOPTS_VALUE
                || sample.dts == AV_NOPTS_VALUE
                || range_invalid) {
                throw std::runtime_error(
                    "invalid mp4: sample index has missing timestamps or offset/size"
                );
            }

            if (codec_id == AV_CODEC_ID_AV1) {
                sample.is_reference = true;
                sample.is_random_access = sample.is_sync;
            } else {
                const NalProperties properties = sample_nal_properties(
                    source.fd(), sample, length_size, codec_id
                );
                sample.is_reference = properties.is_reference;
                sample.is_random_access = properties.is_random_access;
            }
            media.packets.push_back(sample);
        }
        av_packet_unref(packet.get());
    }
    if (media.packets.empty()) {
        throw std::runtime_error("invalid mp4: video track has no samples");
    }

    if (codec_id != AV_CODEC_ID_AV1) {
        media.display_to_decode.reserve(media.packets.size());
        for (const Sample& sample : media.packets) {
            media.display_to_decode.push_back(sample.decode_index);
        }
        std::sort(
            media.display_to_decode.begin(),
            media.display_to_decode.end(),
            [&](int left, int right) {
                const Sample& a = media.packets[static_cast<size_t>(left)];
                const Sample& b = media.packets[static_cast<size_t>(right)];
                if (a.pts != b.pts) {
                    return a.pts < b.pts;
                }
                return a.decode_index < b.decode_index;
            }
        );
        int64_t previous_pts = AV_NOPTS_VALUE;
        for (size_t display = 0; display < media.display_to_decode.size(); ++display) {
            Sample& sample = media.packets.at(
                static_cast<size_t>(media.display_to_decode[display])
            );
            if (display > 0 && sample.pts == previous_pts) {
                throw std::runtime_error(
                    "unsupported: duplicate display timestamps in AVC/HEVC track"
                );
            }
            sample.display_index = static_cast<int>(display);
            previous_pts = sample.pts;
        }
    }
    return media;
}

class Decoder {
public:
    Decoder(
        const AVCodecParameters* parameters,
        AVRational time_base,
        int fd,
        bool enable_state_only = false
    ) : fd_(fd), state_only_enabled_(enable_state_only) {
        const AVCodec* codec = avcodec_find_decoder(parameters->codec_id);
        if (codec == nullptr) {
            throw std::runtime_error("unsupported: decoder is unavailable");
        }
        context_.reset(avcodec_alloc_context3(codec));
        if (!context_) {
            throw std::runtime_error("decode: avcodec_alloc_context3 failed");
        }
        if (state_only_enabled_ && !has_state_only_decoder(context_.get())) {
            throw std::runtime_error(
                "decode: bundled decoder lacks state-only DPB support"
            );
        }
        check_ff(
            avcodec_parameters_to_context(context_.get(), parameters),
            "avcodec_parameters_to_context"
        );
        context_->pkt_timebase = time_base;
        context_->thread_count = 1;
        context_->thread_type = 0;
        if (state_only_enabled_) {
            context_->opaque = &bridge_;
        }
        check_ff(avcodec_open2(context_.get(), codec, nullptr), "avcodec_open2");
        packet_.reset(av_packet_alloc());
        frame_.reset(av_frame_alloc());
        if (!packet_ || !frame_) {
            throw std::runtime_error("decode: packet/frame allocation failed");
        }
    }

    template <typename Callback>
    void run(
        const std::vector<const Sample*>& sequence,
        const std::set<int>* reconstructed,
        Callback&& callback
    ) {
        for (const Sample* sample : sequence) {
            if (state_only_enabled_) {
                if (reconstructed == nullptr) {
                    throw std::runtime_error(
                        "decode: state-only decoder is missing reconstruction mask"
                    );
                }
                bridge_.current_sample = sample->decode_index;
                bridge_.state_only = reconstructed->count(
                    sample->decode_index
                ) == 0;
            }
            av_packet_unref(packet_.get());
            check_ff(av_new_packet(packet_.get(), sample->size), "av_new_packet");
            pread_exact(
                fd_,
                packet_->data,
                static_cast<size_t>(sample->size),
                sample->offset
            );
            packet_->pts = sample->pts;
            packet_->dts = sample->dts;
            packet_->duration = sample->duration;
            packet_->pos = sample->offset;
            if (sample->is_sync) {
                packet_->flags |= AV_PKT_FLAG_KEY;
            }
            send_packet(packet_.get(), callback);
        }

        bridge_.state_only = 0;
        int result = avcodec_send_packet(context_.get(), nullptr);
        if (result < 0 && result != AVERROR_EOF) {
            check_ff(result, "avcodec_send_packet(flush)");
        }
        receive(callback, true);
    }

private:
    template <typename Callback>
    void send_packet(AVPacket* packet, Callback& callback) {
        int result = avcodec_send_packet(context_.get(), packet);
        if (result == AVERROR(EAGAIN)) {
            receive(callback, false);
            result = avcodec_send_packet(context_.get(), packet);
        }
        check_ff(result, "avcodec_send_packet");
        receive(callback, false);
    }

    template <typename Callback>
    void receive(Callback& callback, bool draining) {
        while (true) {
            const int result = avcodec_receive_frame(context_.get(), frame_.get());
            if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) {
                return;
            }
            check_ff(result, "avcodec_receive_frame");
            callback(frame_.get());
            av_frame_unref(frame_.get());
            if (draining && result == AVERROR_EOF) {
                return;
            }
        }
    }

    int fd_;
    bool state_only_enabled_ = false;
    ReferenceGraphBridge bridge_;
    std::unique_ptr<AVCodecContext, CodecContextCloser> context_;
    std::unique_ptr<AVPacket, PacketCloser> packet_;
    std::unique_ptr<AVFrame, FrameCloser> frame_;
};

class ReferenceGraphDecoder {
public:
    ReferenceGraphDecoder(
        const AVCodecParameters* parameters,
        AVRational time_base,
        int fd,
        ReferenceGraphCollector& collector
    ) : fd_(fd), collector_(collector) {
        const AVCodec* codec = avcodec_find_decoder(parameters->codec_id);
        if (codec == nullptr) {
            return;
        }
        context_.reset(avcodec_alloc_context3(codec));
        if (!context_) {
            return;
        }
        // Detect the bundled reference-graph decoder before opening it or
        // submitting a packet.  A stock FFmpeg fallback must not decode one
        // frame merely to probe whether the private callback exists.
        if (!has_state_only_decoder(context_.get())) {
            context_.reset();
            return;
        }
        if (avcodec_parameters_to_context(context_.get(), parameters) < 0) {
            context_.reset();
            return;
        }
        bridge_.opaque = &collector_;
        bridge_.record = collect_reference_graph;
        bridge_.state_only = 1;
        context_->opaque = &bridge_;
        context_->pkt_timebase = time_base;
        context_->thread_count = 1;
        context_->thread_type = 0;
        context_->skip_loop_filter = AVDISCARD_ALL;
        if (avcodec_open2(context_.get(), codec, nullptr) < 0) {
            context_.reset();
            return;
        }
        packet_.reset(av_packet_alloc());
        frame_.reset(av_frame_alloc());
        if (!packet_ || !frame_) {
            context_.reset();
        }
    }

    bool available() const {
        return context_ != nullptr;
    }

    bool run(const MediaIndex& media, int segment_begin, int segment_end) {
        if (!context_
            || segment_begin < 0
            || segment_end < segment_begin
            || static_cast<size_t>(segment_end) >= media.packets.size()) {
            return false;
        }
        const int anchor_display = media.packets.at(
            static_cast<size_t>(segment_begin)
        ).display_index;
        for (int decode = segment_begin; decode <= segment_end; ++decode) {
            const Sample& sample = media.packets.at(static_cast<size_t>(decode));
            if (sample.display_index >= 0
                && sample.display_index < anchor_display) {
                continue;
            }
            bridge_.current_sample = sample.decode_index;
            av_packet_unref(packet_.get());
            if (av_new_packet(packet_.get(), sample.size) < 0) {
                return false;
            }
            try {
                pread_exact(
                    fd_, packet_->data, static_cast<size_t>(sample.size),
                    sample.offset
                );
            } catch (const std::exception&) {
                return false;
            }
            packet_->pts = sample.pts;
            packet_->dts = sample.dts;
            packet_->duration = sample.duration;
            packet_->pos = sample.offset;
            if (sample.is_sync) {
                packet_->flags |= AV_PKT_FLAG_KEY;
            }

            int result = avcodec_send_packet(context_.get(), packet_.get());
            if (result == AVERROR(EAGAIN)) {
                if (!receive()) {
                    return false;
                }
                result = avcodec_send_packet(context_.get(), packet_.get());
            }
            if (result < 0 || !receive()) {
                return false;
            }

        }

        bridge_.current_sample = segment_end;
        const int result = avcodec_send_packet(context_.get(), nullptr);
        if (result < 0 && result != AVERROR_EOF) {
            return false;
        }
        return receive(true);
    }

private:
    bool receive(bool draining = false) {
        while (true) {
            const int result = avcodec_receive_frame(context_.get(), frame_.get());
            if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) {
                return true;
            }
            if (result < 0) {
                return false;
            }
            av_frame_unref(frame_.get());
            if (draining && result == AVERROR_EOF) {
                return true;
            }
        }
    }

    int fd_;
    ReferenceGraphCollector& collector_;
    ReferenceGraphBridge bridge_;
    std::unique_ptr<AVCodecContext, CodecContextCloser> context_;
    std::unique_ptr<AVPacket, PacketCloser> packet_;
    std::unique_ptr<AVFrame, FrameCloser> frame_;
};

bool populate_reference_graph_segment(
    MediaIndex& media,
    const AVCodecParameters* parameters,
    const InputSource& source,
    int segment_begin,
    int segment_end
) {
    if ((media.codec_id != AV_CODEC_ID_H264
         && media.codec_id != AV_CODEC_ID_HEVC)
        || segment_begin < 0
        || segment_end < segment_begin
        || static_cast<size_t>(segment_end) >= media.packets.size()) {
        return false;
    }

    ReferenceGraphCollector collector(media.packets.size());
    ReferenceGraphDecoder decoder(
        parameters, media.time_base, source.fd(), collector
    );
    if (!decoder.available()
        || !decoder.run(media, segment_begin, segment_end)
        || collector.invalid) {
        return false;
    }
    const int anchor_display = media.packets.at(
        static_cast<size_t>(segment_begin)
    ).display_index;
    for (int decode = segment_begin; decode <= segment_end; ++decode) {
        const Sample& sample = media.packets.at(static_cast<size_t>(decode));
        const bool leading_picture = sample.display_index >= 0
            && sample.display_index < anchor_display;
        if (!leading_picture
            && !collector.seen.at(static_cast<size_t>(decode))) {
            return false;
        }
    }

    std::vector<std::vector<int>> direct_references(media.packets.size());
    int parsed_packets = 0;
    for (int decode = segment_begin; decode <= segment_end; ++decode) {
        const Sample& sample = media.packets.at(static_cast<size_t>(decode));
        const bool leading_picture = sample.display_index >= 0
            && sample.display_index < anchor_display;
        if (leading_picture) {
            continue;
        }
        ++parsed_packets;
        const std::set<int>& references = collector.references.at(
            static_cast<size_t>(decode)
        );
        for (int reference : references) {
            const Sample& dependency = media.packets.at(
                static_cast<size_t>(reference)
            );
            if (dependency.display_index >= 0
                && dependency.display_index < anchor_display) {
                return false;
            }
        }
        direct_references[static_cast<size_t>(decode)].assign(
            references.begin(), references.end()
        );
    }

    media.direct_references = std::move(direct_references);
    media.reference_graph_available = true;
    media.reference_graph_segment_begin = segment_begin;
    media.reference_graph_segment_end = segment_end;
    media.reference_graph_packets = parsed_packets;
    return true;
}

bool build_reference_graph(
    MediaIndex& media,
    const AVCodecParameters* parameters,
    const InputSource& source
) {
    if (media.packets.empty()) {
        return false;
    }
    media.reference_graph_attempts = 1;
    return populate_reference_graph_segment(
        media, parameters, source, 0,
        static_cast<int>(media.packets.size()) - 1
    );
}

bool is_reference_graph_start(const MediaIndex& media, const Sample& sample) {
    if (media.codec_id == AV_CODEC_ID_HEVC) {
        return sample.is_random_access;
    }
    return sample.is_random_access || sample.is_sync;
}

int preceding_reference_graph_start(
    const MediaIndex& media,
    int decode_index
) {
    for (int decode = decode_index; decode > 0; --decode) {
        if (is_reference_graph_start(
                media, media.packets.at(static_cast<size_t>(decode))
            )) {
            return decode;
        }
    }
    return 0;
}

int preceding_reference_graph_start_for_display(
    const MediaIndex& media,
    int display_index
) {
    for (int display = display_index; display >= 0; --display) {
        const int decode = media.display_to_decode.at(
            static_cast<size_t>(display)
        );
        if (is_reference_graph_start(
                media, media.packets.at(static_cast<size_t>(decode))
            )) {
            return decode;
        }
    }
    return 0;
}

bool build_reference_graph_for_targets(
    MediaIndex& media,
    const AVCodecParameters* parameters,
    const InputSource& source,
    const std::vector<int>& targets
) {
    int last_target_decode = -1;
    for (int target : targets) {
        const int decode = media.display(target).decode_index;
        last_target_decode = std::max(last_target_decode, decode);
    }
    if (last_target_decode < 0) {
        return false;
    }

    // Open GOPs place leading pictures after the next I/CRA sample in decode
    // order even though those pictures are displayed before that anchor.  Pick
    // the nearest anchor in display order so the segment never starts from a
    // future random-access picture relative to the earliest requested frame.
    int segment_begin = preceding_reference_graph_start_for_display(
        media, targets.front()
    );
    constexpr int max_candidate_attempts = 8;
    int attempts = 0;
    while (true) {
        ++attempts;
        if (populate_reference_graph_segment(
                media, parameters, source,
                segment_begin, last_target_decode
            )) {
            media.reference_graph_attempts = attempts;
            return true;
        }
        if (segment_begin == 0) {
            media.reference_graph_attempts = attempts;
            return false;
        }
        if (attempts >= max_candidate_attempts) {
            segment_begin = 0;
        } else {
            segment_begin = preceding_reference_graph_start(
                media, segment_begin - 1
            );
        }
    }
}

std::vector<const Sample*> all_samples(const MediaIndex& media) {
    std::vector<const Sample*> result;
    result.reserve(media.packets.size());
    for (const Sample& sample : media.packets) {
        result.push_back(&sample);
    }
    return result;
}

struct DecodeGroup {
    std::vector<int> targets;
    std::set<int> selected_decode;
    int sequence_begin = -1;
    int sequence_end = -1;
};

bool intersects(const std::set<int>& left, const std::set<int>& right) {
    auto a = left.begin();
    auto b = right.begin();
    while (a != left.end() && b != right.end()) {
        if (*a == *b) {
            return true;
        }
        if (*a < *b) {
            ++a;
        } else {
            ++b;
        }
    }
    return false;
}

std::set<int> breadth_first_closure(
    const MediaIndex& media,
    int target_decode
) {
    if (!media.reference_graph_available
        || media.direct_references.size() != media.packets.size()
        || target_decode < 0
        || static_cast<size_t>(target_decode) >= media.packets.size()) {
        throw std::runtime_error("decode: reference graph is unavailable");
    }

    std::set<int> selected{target_decode};
    std::deque<int> pending{target_decode};
    while (!pending.empty()) {
        const int current = pending.front();
        pending.pop_front();
        for (int reference : media.direct_references.at(
                 static_cast<size_t>(current)
             )) {
            if (reference < 0 || reference >= current) {
                throw std::runtime_error(
                    "decode: reference graph is not decode-order acyclic"
                );
            }
            if (selected.insert(reference).second) {
                pending.push_back(reference);
            }
        }
    }
    return selected;
}

std::vector<DecodeGroup> reference_graph_groups(
    const MediaIndex& media,
    const std::vector<int>& targets
) {
    std::vector<DecodeGroup> groups;
    for (int target : targets) {
        const int target_decode = media.display(target).decode_index;
        std::set<int> closure = breadth_first_closure(media, target_decode);

        size_t destination = groups.size();
        for (size_t index = 0; index < groups.size(); ++index) {
            if (intersects(groups[index].selected_decode, closure)) {
                destination = index;
                break;
            }
        }
        if (destination == groups.size()) {
            DecodeGroup group;
            group.targets.push_back(target);
            group.selected_decode = std::move(closure);
            groups.push_back(std::move(group));
            continue;
        }

        DecodeGroup& group = groups[destination];
        group.targets.push_back(target);
        group.selected_decode.insert(closure.begin(), closure.end());

        // A newly merged closure can connect groups that were disjoint before
        // this target was added.  Collapse the full connected component so a
        // shared reference picture is submitted to exactly one decoder.
        for (size_t index = destination + 1; index < groups.size();) {
            if (!intersects(
                    group.selected_decode, groups[index].selected_decode
                )) {
                ++index;
                continue;
            }
            group.targets.insert(
                group.targets.end(),
                groups[index].targets.begin(), groups[index].targets.end()
            );
            group.selected_decode.insert(
                groups[index].selected_decode.begin(),
                groups[index].selected_decode.end()
            );
            groups.erase(groups.begin() + static_cast<std::ptrdiff_t>(index));
        }
    }

    for (DecodeGroup& group : groups) {
        std::sort(group.targets.begin(), group.targets.end());
        group.targets.erase(
            std::unique(group.targets.begin(), group.targets.end()),
            group.targets.end()
        );
        if (group.selected_decode.empty()) {
            throw std::runtime_error(
                "decode: reference graph produced an empty closure"
            );
        }
        group.sequence_begin = std::max(
            0, media.reference_graph_segment_begin
        );
        group.sequence_end = *group.selected_decode.rbegin();
    }
    std::sort(groups.begin(), groups.end(), [](const DecodeGroup& a, const DecodeGroup& b) {
        return a.sequence_begin < b.sequence_begin
            || (a.sequence_begin == b.sequence_begin
                && a.sequence_end < b.sequence_end);
    });

    // Separate pixel closures may need the same header-only warm-up interval.
    // Merge overlapping execution ranges so every compressed packet is sent
    // at most once in one decode call, while retaining the exact union of
    // frames that need pixel reconstruction.
    std::vector<DecodeGroup> merged;
    for (DecodeGroup& group : groups) {
        if (merged.empty()
            || group.sequence_begin > merged.back().sequence_end) {
            merged.push_back(std::move(group));
            continue;
        }
        DecodeGroup& destination = merged.back();
        destination.sequence_end = std::max(
            destination.sequence_end, group.sequence_end
        );
        destination.targets.insert(
            destination.targets.end(), group.targets.begin(), group.targets.end()
        );
        destination.selected_decode.insert(
            group.selected_decode.begin(), group.selected_decode.end()
        );
        std::sort(destination.targets.begin(), destination.targets.end());
        destination.targets.erase(
            std::unique(destination.targets.begin(), destination.targets.end()),
            destination.targets.end()
        );
    }
    groups = std::move(merged);

    std::set<int> globally_selected;
    for (const DecodeGroup& group : groups) {
        for (int decode : group.selected_decode) {
            if (!globally_selected.insert(decode).second) {
                throw std::runtime_error(
                    "decode: a compressed frame occurs in multiple closure groups"
                );
            }
        }
    }
    return groups;
}

std::vector<const Sample*> contiguous_closure(
    const MediaIndex& media,
    const std::vector<int>& targets
) {
    const int first_target = targets.front();
    int previous_random_access = -1;
    for (int display = 0; display <= first_target; ++display) {
        if (media.display(display).is_random_access) {
            previous_random_access = display;
        }
    }
    if (previous_random_access < 0) {
        return all_samples(media);
    }
    const int first_decode = media.display(previous_random_access).decode_index;
    int last_decode = -1;
    for (int target : targets) {
        last_decode = std::max(last_decode, media.display(target).decode_index);
    }
    if (last_decode < first_decode) {
        return all_samples(media);
    }
    std::vector<const Sample*> sequence;
    sequence.reserve(static_cast<size_t>(last_decode - first_decode + 1));
    for (int decode = first_decode; decode <= last_decode; ++decode) {
        sequence.push_back(&media.packets.at(static_cast<size_t>(decode)));
    }
    return sequence;
}

struct RgbFrame {
    int width = 0;
    int height = 0;
    int stride = 0;
    std::unique_ptr<uint8_t, AvBufferCloser> pixels;
};

class RgbConverter {
public:
    RgbFrame convert(const AVFrame* frame, int expected_width, int expected_height) {
        if (frame->width != expected_width || frame->height != expected_height) {
            throw std::runtime_error("unsupported: video resolution changes mid-stream");
        }
        SwsContext* updated = sws_getCachedContext(
            context_.release(),
            frame->width,
            frame->height,
            static_cast<AVPixelFormat>(frame->format),
            frame->width,
            frame->height,
            AV_PIX_FMT_RGB24,
            SWS_BILINEAR,
            nullptr,
            nullptr,
            nullptr
        );
        if (updated == nullptr) {
            throw std::runtime_error("decode: sws_getCachedContext failed");
        }
        context_.reset(updated);

        // Optimized swscale RGB writers may touch a complete SIMD block past
        // the visible row. Keep every row aligned and independently padded,
        // then copy only visible pixels into the tightly packed Python result.
        constexpr int destination_alignment = 64;
        if (frame->width > (std::numeric_limits<int>::max()
                            - 2 * destination_alignment + 1) / 3) {
            throw std::runtime_error("decode: RGB row size overflows int");
        }
        const int packed_stride = frame->width * 3;
        const int aligned_stride = (
            packed_stride + destination_alignment - 1
        ) & ~(destination_alignment - 1);
        const int padded_stride = aligned_stride + destination_alignment;
        const size_t height = static_cast<size_t>(frame->height);
        const size_t stride = static_cast<size_t>(padded_stride);
        if (height > (
                std::numeric_limits<size_t>::max()
                - AV_INPUT_BUFFER_PADDING_SIZE
            ) / stride) {
            throw std::runtime_error("decode: RGB image size overflows size_t");
        }
        const size_t buffer_size = stride * height
            + AV_INPUT_BUFFER_PADDING_SIZE;

        RgbFrame output;
        output.width = frame->width;
        output.height = frame->height;
        output.stride = padded_stride;
        output.pixels.reset(static_cast<uint8_t*>(av_mallocz(buffer_size)));
        if (!output.pixels) {
            throw std::runtime_error("decode: RGB frame allocation failed");
        }
        uint8_t* destination[4] = {
            output.pixels.get(), nullptr, nullptr, nullptr
        };
        int destination_stride[4] = {padded_stride, 0, 0, 0};
        const int rows = sws_scale(
            context_.get(),
            frame->data,
            frame->linesize,
            0,
            frame->height,
            destination,
            destination_stride
        );
        if (rows != frame->height) {
            throw std::runtime_error("decode: sws_scale returned incomplete image");
        }
        return output;
    }

private:
    std::unique_ptr<SwsContext, SwsCloser> context_;
};

struct DecodeAttempt {
    std::vector<std::optional<RgbFrame>> frames;
    int packets_sent = 0;
    int state_only_packets = 0;
    int frames_reconstructed = 0;
    int frames_output = 0;
    int64_t compressed_bytes = 0;
    int64_t reconstructed_bytes = 0;
    double decode_ms = 0.0;
};

bool complete(const DecodeAttempt& attempt, size_t expected_frames);

DecodeAttempt decode_sequence_by_pts(
    const InputSource& source,
    AVStream* stream,
    const MediaIndex& media,
    const std::vector<const Sample*>& sequence,
    const std::vector<int>& targets,
    const std::set<int>* reconstructed = nullptr
) {
    std::map<int64_t, size_t> target_pts;
    for (size_t slot = 0; slot < targets.size(); ++slot) {
        target_pts.emplace(media.display(targets[slot]).pts, slot);
    }
    DecodeAttempt result;
    result.frames.resize(targets.size());
    result.packets_sent = static_cast<int>(sequence.size());
    std::set<int64_t> reconstructed_pts;
    for (const Sample* sample : sequence) {
        result.compressed_bytes += sample->size;
        if (reconstructed != nullptr
            && reconstructed->count(sample->decode_index) != 0) {
            ++result.frames_reconstructed;
            result.reconstructed_bytes += sample->size;
            reconstructed_pts.insert(sample->pts);
        } else if (reconstructed != nullptr) {
            ++result.state_only_packets;
        }
    }
    if (reconstructed == nullptr) {
        result.frames_reconstructed = result.packets_sent;
        result.reconstructed_bytes = result.compressed_bytes;
    }
    RgbConverter converter;
    Decoder decoder(
        stream->codecpar, stream->time_base, source.fd(),
        reconstructed != nullptr
    );
    const auto started = std::chrono::steady_clock::now();
    decoder.run(sequence, reconstructed, [&](const AVFrame* frame) {
        int64_t timestamp = frame->best_effort_timestamp;
        if (timestamp == AV_NOPTS_VALUE) {
            timestamp = frame->pts;
        }
        if (reconstructed != nullptr
            && reconstructed_pts.count(timestamp) == 0) {
            return;
        }
        ++result.frames_output;
        const auto found = target_pts.find(timestamp);
        if (found != target_pts.end() && !result.frames[found->second].has_value()) {
            result.frames[found->second] = converter.convert(
                frame, media.width, media.height
            );
        }
    });
    result.decode_ms = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - started
    ).count();
    return result;
}

DecodeAttempt decode_all_by_ordinal(
    const InputSource& source,
    AVStream* stream,
    const MediaIndex& media,
    const std::vector<int>& targets,
    bool convert_pixels
) {
    std::map<int, size_t> target_slots;
    for (size_t slot = 0; slot < targets.size(); ++slot) {
        target_slots.emplace(targets[slot], slot);
    }
    DecodeAttempt result;
    result.frames.resize(targets.size());
    const std::vector<const Sample*> sequence = all_samples(media);
    result.packets_sent = static_cast<int>(sequence.size());
    result.frames_reconstructed = result.packets_sent;
    for (const Sample* sample : sequence) {
        result.compressed_bytes += sample->size;
    }
    result.reconstructed_bytes = result.compressed_bytes;
    RgbConverter converter;
    Decoder decoder(stream->codecpar, stream->time_base, source.fd());
    int display_index = 0;
    const auto started = std::chrono::steady_clock::now();
    decoder.run(sequence, nullptr, [&](const AVFrame* frame) {
        const auto found = target_slots.find(display_index);
        if (convert_pixels && found != target_slots.end()) {
            result.frames[found->second] = converter.convert(
                frame, media.width, media.height
            );
        }
        ++display_index;
        ++result.frames_output;
    });
    result.decode_ms = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - started
    ).count();
    return result;
}

DecodeAttempt decode_groups_by_pts(
    const InputSource& source,
    AVStream* stream,
    const MediaIndex& media,
    const std::vector<int>& targets,
    const std::vector<DecodeGroup>& groups
) {
    DecodeAttempt result;
    result.frames.resize(targets.size());
    for (const DecodeGroup& group : groups) {
        std::vector<const Sample*> sequence;
        sequence.reserve(
            static_cast<size_t>(group.sequence_end - group.sequence_begin + 1)
        );
        const int anchor_display = media.packets.at(
            static_cast<size_t>(group.sequence_begin)
        ).display_index;
        for (int decode = group.sequence_begin;
             decode <= group.sequence_end;
             ++decode) {
            const Sample& sample = media.packets.at(
                static_cast<size_t>(decode)
            );
            if (sample.display_index >= 0
                && sample.display_index < anchor_display) {
                continue;
            }
            sequence.push_back(&sample);
        }
        DecodeAttempt local = decode_sequence_by_pts(
            source, stream, media, sequence, group.targets,
            &group.selected_decode
        );
        result.packets_sent += local.packets_sent;
        result.state_only_packets += local.state_only_packets;
        result.frames_reconstructed += local.frames_reconstructed;
        result.frames_output += local.frames_output;
        result.compressed_bytes += local.compressed_bytes;
        result.reconstructed_bytes += local.reconstructed_bytes;
        result.decode_ms += local.decode_ms;
        if (!complete(local, group.targets.size())) {
            return result;
        }
        for (size_t slot = 0; slot < group.targets.size(); ++slot) {
            const size_t destination = static_cast<size_t>(
                std::lower_bound(
                    targets.begin(), targets.end(), group.targets[slot]
                ) - targets.begin()
            );
            result.frames[destination] = std::move(local.frames[slot]);
        }
    }
    return result;
}

bool complete(const DecodeAttempt& attempt, size_t expected_frames) {
    return attempt.frames.size() == expected_frames && std::all_of(
        attempt.frames.begin(),
        attempt.frames.end(),
        [](const std::optional<RgbFrame>& frame) { return frame.has_value(); }
    );
}

struct DecodeResult {
    int width = 0;
    int height = 0;
    int total_frames = 0;
    int packets_sent = 0;
    int state_only_packets = 0;
    int frames_reconstructed = 0;
    int frames_output = 0;
    int64_t compressed_bytes = 0;
    int64_t reconstructed_bytes = 0;
    double index_ms = 0.0;
    double decode_ms = 0.0;
    int reference_edges = 0;
    int closure_groups = 0;
    int reference_graph_segment_begin = -1;
    int reference_graph_segment_end = -1;
    int reference_graph_packets = 0;
    int reference_graph_attempts = 0;
    bool reference_graph_available = false;
    std::string codec;
    std::string closure_mode;
    std::vector<RgbFrame> unique_frames;
    std::vector<size_t> original_to_unique;
};

DecodeResult decode_native(
    const InputSource& source,
    const std::vector<int>& requested
) {
    if (requested.empty()) {
        throw std::runtime_error("decode: indices must not be empty");
    }
    for (int index : requested) {
        if (index < 0) {
            throw std::runtime_error("decode: frame indices must be non-negative");
        }
    }
    std::vector<int> targets = requested;
    std::sort(targets.begin(), targets.end());
    targets.erase(std::unique(targets.begin(), targets.end()), targets.end());

    const auto index_started = std::chrono::steady_clock::now();
    auto format = open_format(source);
    MediaIndex media = build_index(format.get(), source);
    AVStream* stream = format->streams[media.video_stream];
    int total_frames = 0;
    bool reference_graph_available = false;
    if (media.codec_id == AV_CODEC_ID_H264
        || media.codec_id == AV_CODEC_ID_HEVC) {
        total_frames = static_cast<int>(media.display_to_decode.size());
        if (targets.back() >= total_frames) {
            throw std::runtime_error(
                "decode: frame index " + std::to_string(targets.back())
                + " exceeds video length " + std::to_string(total_frames)
            );
        }
        reference_graph_available = build_reference_graph_for_targets(
            media, stream->codecpar, source, targets
        );
    }
    const double index_ms = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - index_started
    ).count();

    DecodeAttempt attempt;
    std::vector<const Sample*> sequence;
    std::string closure_mode;
    int closure_groups = 0;
    if (reference_graph_available) {
        const std::vector<DecodeGroup> groups = reference_graph_groups(
            media, targets
        );
        closure_groups = static_cast<int>(groups.size());
        attempt = decode_groups_by_pts(
            source, stream, media, targets, groups
        );
        closure_mode = "reference_graph_bfs";
    } else if (media.codec_id == AV_CODEC_ID_AV1) {
        attempt = decode_all_by_ordinal(source, stream, media, targets, true);
        total_frames = attempt.frames_output;
        closure_mode = "complete_packets_av1";
        closure_groups = 1;
        sequence = all_samples(media);
    } else {
        sequence = contiguous_closure(media, targets);
        attempt = decode_sequence_by_pts(
            source, stream, media, sequence, targets
        );
        closure_mode = "contiguous_no_reference_graph";
        closure_groups = 1;
    }

    if (targets.back() >= total_frames) {
        throw std::runtime_error(
            "decode: frame index " + std::to_string(targets.back())
            + " exceeds video length " + std::to_string(total_frames)
        );
    }
    if (!complete(attempt, targets.size())) {
        throw std::runtime_error(
            "decode: decoder did not return every requested display frame"
        );
    }

    DecodeResult result;
    result.width = media.width;
    result.height = media.height;
    result.total_frames = total_frames;
    result.packets_sent = attempt.packets_sent;
    result.state_only_packets = attempt.state_only_packets;
    result.frames_reconstructed = attempt.frames_reconstructed;
    result.frames_output = attempt.frames_output;
    result.compressed_bytes = attempt.compressed_bytes;
    result.reconstructed_bytes = attempt.reconstructed_bytes;
    result.index_ms = index_ms;
    result.decode_ms = attempt.decode_ms;
    result.reference_edges = 0;
    for (const std::vector<int>& references : media.direct_references) {
        result.reference_edges += static_cast<int>(references.size());
    }
    result.closure_groups = closure_groups;
    result.reference_graph_segment_begin = media.reference_graph_segment_begin;
    result.reference_graph_segment_end = media.reference_graph_segment_end;
    result.reference_graph_packets = media.reference_graph_packets;
    result.reference_graph_attempts = media.reference_graph_attempts;
    result.reference_graph_available = reference_graph_available;
    result.codec = media.codec_name;
    result.closure_mode = closure_mode;
    result.unique_frames.reserve(targets.size());
    for (std::optional<RgbFrame>& frame : attempt.frames) {
        result.unique_frames.push_back(std::move(frame.value()));
    }
    result.original_to_unique.reserve(requested.size());
    for (int index : requested) {
        result.original_to_unique.push_back(
            static_cast<size_t>(
                std::lower_bound(targets.begin(), targets.end(), index)
                - targets.begin()
            )
        );
    }
    return result;
}

struct InspectResult {
    std::string codec;
    int width = 0;
    int height = 0;
    int sample_count = 0;
    int frame_count = 0;
    double fps = 0.0;
    double duration_seconds = 0.0;
    int reference_edges = 0;
    bool reference_graph_available = false;
    std::string closure_policy;
};

InspectResult inspect_native(const InputSource& source) {
    auto format = open_format(source);
    MediaIndex media = build_index(format.get(), source);
    AVStream* stream = format->streams[media.video_stream];
    InspectResult result;
    result.reference_graph_available = build_reference_graph(
        media, stream->codecpar, source
    );
    result.codec = media.codec_name;
    result.width = media.width;
    result.height = media.height;
    result.sample_count = static_cast<int>(media.packets.size());
    result.fps = media.frame_rate.den != 0 ? av_q2d(media.frame_rate) : 0.0;
    result.duration_seconds = media.duration != AV_NOPTS_VALUE
        ? static_cast<double>(media.duration) * av_q2d(media.time_base)
        : 0.0;
    for (const std::vector<int>& references : media.direct_references) {
        result.reference_edges += static_cast<int>(references.size());
    }
    if (result.reference_graph_available) {
        result.frame_count = static_cast<int>(media.display_to_decode.size());
        result.closure_policy = "reference_graph_bfs";
    } else if (media.codec_id == AV_CODEC_ID_AV1) {
        const std::vector<int> no_targets;
        const DecodeAttempt counted = decode_all_by_ordinal(
            source, stream, media, no_targets, false
        );
        result.frame_count = counted.frames_output;
        result.closure_policy = "complete_packets_av1";
    } else {
        result.frame_count = static_cast<int>(media.display_to_decode.size());
        result.closure_policy = "contiguous_no_reference_graph";
    }
    return result;
}

struct OutputFormatCloser {
    void operator()(AVFormatContext* value) const {
        if (value != nullptr) {
            if (value->pb != nullptr && !(value->oformat->flags & AVFMT_NOFILE)) {
                avio_closep(&value->pb);
            }
            avformat_free_context(value);
        }
    }
};

struct ArrayView {
    const uint8_t* data = nullptr;
    ssize_t frames = 0;
    ssize_t height = 0;
    ssize_t width = 0;
    ssize_t frame_stride = 0;
    ssize_t row_stride = 0;
    ssize_t pixel_stride = 0;
    ssize_t channel_stride = 0;
};

void drain_encoder(
    AVCodecContext* encoder,
    AVFormatContext* format,
    AVStream* stream,
    AVPacket* packet
) {
    while (true) {
        const int result = avcodec_receive_packet(encoder, packet);
        if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) {
            return;
        }
        check_ff(result, "avcodec_receive_packet", "encode:");
        av_packet_rescale_ts(packet, encoder->time_base, stream->time_base);
        packet->stream_index = stream->index;
        check_ff(
            av_interleaved_write_frame(format, packet),
            "av_interleaved_write_frame",
            "encode:"
        );
        av_packet_unref(packet);
    }
}

void transcode_native(
    const ArrayView& input,
    double fps,
    const std::string& mode,
    double crf,
    const std::string& output_path
) {
    if (input.frames <= 0 || input.height <= 0 || input.width <= 0) {
        throw std::runtime_error("encode: frames must have positive T/H/W dimensions");
    }
    if (input.width > std::numeric_limits<int>::max()
        || input.height > std::numeric_limits<int>::max()) {
        throw std::runtime_error("encode: video dimensions are too large");
    }
    if ((input.width & 1) != 0 || (input.height & 1) != 0) {
        throw std::runtime_error("encode: yuv420p requires even width and height");
    }
    if (!std::isfinite(fps) || fps <= 0.0 || fps > 1000.0) {
        throw std::runtime_error("encode: fps must be finite and in (0, 1000]");
    }
    if (!std::isfinite(crf) || crf < 0.0 || crf > 51.0) {
        throw std::runtime_error("encode: CRF must be finite and in [0, 51]");
    }
    const bool is_h265 = mode == "base265" || mode == "fast265";
    const bool fast = mode == "fast264" || mode == "fast265"
        || mode == "ufast264";
    const bool cavlc = mode == "ufast264";
    if (mode != "base264" && mode != "base265" && !fast) {
        throw std::runtime_error(
            "encode: mode must be base264, base265, fast264, fast265, or ufast264"
        );
    }

    AVFormatContext* raw_format = nullptr;
    check_ff(
        avformat_alloc_output_context2(
            &raw_format, nullptr, "mp4", output_path.c_str()
        ),
        "avformat_alloc_output_context2",
        "encode:"
    );
    if (raw_format == nullptr) {
        throw std::runtime_error("encode: MP4 muxer is unavailable");
    }
    std::unique_ptr<AVFormatContext, OutputFormatCloser> format(raw_format);

    const char* encoder_name = is_h265 ? "libx265" : "libx264";
    const AVCodec* codec = avcodec_find_encoder_by_name(encoder_name);
    if (codec == nullptr) {
        throw std::runtime_error(
            "encode: FFmpeg build has no " + std::string(encoder_name)
            + " encoder"
        );
    }
    AVStream* stream = avformat_new_stream(format.get(), nullptr);
    if (stream == nullptr) {
        throw std::runtime_error("encode: avformat_new_stream failed");
    }
    std::unique_ptr<AVCodecContext, CodecContextCloser> encoder(
        avcodec_alloc_context3(codec)
    );
    if (!encoder) {
        throw std::runtime_error("encode: avcodec_alloc_context3 failed");
    }

    const AVRational frame_rate = av_d2q(fps, 1000000);
    encoder->codec_type = AVMEDIA_TYPE_VIDEO;
    encoder->codec_id = codec->id;
    encoder->width = static_cast<int>(input.width);
    encoder->height = static_cast<int>(input.height);
    encoder->pix_fmt = AV_PIX_FMT_YUV420P;
    encoder->time_base = av_inv_q(frame_rate);
    encoder->framerate = frame_rate;
    encoder->profile = is_h265 ? AV_PROFILE_HEVC_MAIN : AV_PROFILE_H264_MAIN;
    encoder->gop_size = fast ? 8 : 32;
    encoder->max_b_frames = fast ? 7 : 3;
    if (format->oformat->flags & AVFMT_GLOBALHEADER) {
        encoder->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
    }

    AVDictionary* raw_options = nullptr;
    av_dict_set(&raw_options, "preset", "medium", 0);
    av_dict_set(&raw_options, "profile", "main", 0);
    const std::string crf_text = std::to_string(crf);
    av_dict_set(&raw_options, "crf", crf_text.c_str(), 0);
    if (is_h265) {
        const char* x265_params = fast
            ? "keyint=8:min-keyint=8:scenecut=0:open-gop=1:bframes=7:"
              "b-adapt=0:b-pyramid=0:ref=1:log-level=error"
            : "keyint=32:min-keyint=32:scenecut=0:open-gop=0:"
              "bframes=3:b-adapt=1:b-pyramid=1:log-level=error";
        av_dict_set(&raw_options, "x265-params", x265_params, 0);
    } else {
        const char* x264_params = fast
            ? (cavlc
                ? "keyint=8:min-keyint=8:scenecut=0:open-gop=1:bframes=7:"
                  "b-adapt=0:b-pyramid=none:cabac=0:ref=1:force-cfr=1"
                : "keyint=8:min-keyint=8:scenecut=0:open-gop=1:bframes=7:"
                  "b-adapt=0:b-pyramid=none:cabac=1:ref=1:force-cfr=1")
            : "keyint=32:min-keyint=32:scenecut=0:open-gop=0:"
              "bframes=3:b-adapt=1:b-pyramid=normal:cabac=1";
        av_dict_set(&raw_options, "x264-params", x264_params, 0);
    }
    const int opened = avcodec_open2(encoder.get(), codec, &raw_options);
    av_dict_free(&raw_options);
    const std::string open_operation =
        "avcodec_open2(" + std::string(encoder_name) + ")";
    check_ff(
        opened,
        open_operation.c_str(),
        "encode:"
    );

    check_ff(
        avcodec_parameters_from_context(stream->codecpar, encoder.get()),
        "avcodec_parameters_from_context",
        "encode:"
    );
    if (is_h265) {
        stream->codecpar->codec_tag = MKTAG('h', 'v', 'c', '1');
    }
    stream->time_base = encoder->time_base;
    stream->avg_frame_rate = frame_rate;
    stream->r_frame_rate = frame_rate;

    if (!(format->oformat->flags & AVFMT_NOFILE)) {
        check_ff(
            avio_open(&format->pb, output_path.c_str(), AVIO_FLAG_WRITE),
            "avio_open",
            "encode:"
        );
    }
    AVDictionary* mux_options = nullptr;
    av_dict_set(&mux_options, "movflags", "+faststart", 0);
    const int header_result = avformat_write_header(format.get(), &mux_options);
    av_dict_free(&mux_options);
    check_ff(header_result, "avformat_write_header", "encode:");

    std::unique_ptr<AVFrame, FrameCloser> frame(av_frame_alloc());
    std::unique_ptr<AVPacket, PacketCloser> packet(av_packet_alloc());
    if (!frame || !packet) {
        throw std::runtime_error("encode: frame/packet allocation failed");
    }
    frame->format = encoder->pix_fmt;
    frame->width = encoder->width;
    frame->height = encoder->height;
    check_ff(av_frame_get_buffer(frame.get(), 32), "av_frame_get_buffer", "encode:");

    std::unique_ptr<SwsContext, SwsCloser> converter(sws_getContext(
        encoder->width,
        encoder->height,
        AV_PIX_FMT_RGB24,
        encoder->width,
        encoder->height,
        encoder->pix_fmt,
        SWS_BILINEAR,
        nullptr,
        nullptr,
        nullptr
    ));
    if (!converter) {
        throw std::runtime_error("encode: sws_getContext failed");
    }

    std::vector<uint8_t> packed;
    const bool input_is_packed = input.channel_stride == 1
        && input.pixel_stride == 3
        && input.row_stride == input.width * 3;
    if (!input_is_packed) {
        packed.resize(
            static_cast<size_t>(input.width)
            * static_cast<size_t>(input.height)
            * 3
        );
    }

    for (ssize_t index = 0; index < input.frames; ++index) {
        check_ff(av_frame_make_writable(frame.get()), "av_frame_make_writable", "encode:");
        const uint8_t* source_frame = input.data + index * input.frame_stride;
        int source_stride = static_cast<int>(input.row_stride);
        if (!input_is_packed) {
            for (ssize_t row = 0; row < input.height; ++row) {
                for (ssize_t column = 0; column < input.width; ++column) {
                    for (ssize_t channel = 0; channel < 3; ++channel) {
                        packed[
                            (static_cast<size_t>(row) * static_cast<size_t>(input.width)
                             + static_cast<size_t>(column)) * 3
                            + static_cast<size_t>(channel)
                        ] = *(source_frame
                            + row * input.row_stride
                            + column * input.pixel_stride
                            + channel * input.channel_stride);
                    }
                }
            }
            source_frame = packed.data();
            source_stride = static_cast<int>(input.width * 3);
        }
        const uint8_t* source_planes[4] = {
            source_frame, nullptr, nullptr, nullptr
        };
        const int source_strides[4] = {source_stride, 0, 0, 0};
        const int converted = sws_scale(
            converter.get(),
            source_planes,
            source_strides,
            0,
            encoder->height,
            frame->data,
            frame->linesize
        );
        if (converted != encoder->height) {
            throw std::runtime_error("encode: RGB to yuv420p conversion failed");
        }
        frame->pts = index;
        frame->pict_type = AV_PICTURE_TYPE_NONE;
        if (fast && index > 0) {
            if (index == input.frames - 1 && index % 8 != 0) {
                frame->pict_type = AV_PICTURE_TYPE_I;
            } else if (index % 8 != 0) {
                frame->pict_type = AV_PICTURE_TYPE_B;
            }
        }
        check_ff(
            avcodec_send_frame(encoder.get(), frame.get()),
            "avcodec_send_frame",
            "encode:"
        );
        drain_encoder(encoder.get(), format.get(), stream, packet.get());
    }

    int flush_result = avcodec_send_frame(encoder.get(), nullptr);
    if (flush_result < 0 && flush_result != AVERROR_EOF) {
        check_ff(flush_result, "avcodec_send_frame(flush)", "encode:");
    }
    drain_encoder(encoder.get(), format.get(), stream, packet.get());
    check_ff(av_write_trailer(format.get()), "av_write_trailer", "encode:");
}

py::dict decode_info(const DecodeResult& result) {
    py::dict info;
    info["codec"] = result.codec;
    info["width"] = result.width;
    info["height"] = result.height;
    info["total_frames"] = result.total_frames;
    info["requested_frames"] = result.original_to_unique.size();
    info["unique_requested_frames"] = result.unique_frames.size();
    info["packets_sent"] = result.packets_sent;
    info["state_only_packets"] = result.state_only_packets;
    info["frames_reconstructed"] = result.frames_reconstructed;
    info["frames_output"] = result.frames_output;
    info["compressed_bytes"] = result.compressed_bytes;
    info["reconstructed_bytes"] = result.reconstructed_bytes;
    info["index_seconds"] = result.index_ms / 1000.0;
    info["decode_seconds"] = result.decode_ms / 1000.0;
    info["reference_graph_available"] = result.reference_graph_available;
    info["reference_edges"] = result.reference_edges;
    info["closure_groups"] = result.closure_groups;
    info["reference_graph_segment_begin"] =
        result.reference_graph_segment_begin;
    info["reference_graph_segment_end"] =
        result.reference_graph_segment_end;
    info["reference_graph_packets"] = result.reference_graph_packets;
    info["reference_graph_attempts"] = result.reference_graph_attempts;
    info["closure_mode"] = result.closure_mode;
    return info;
}

}  // namespace

PYBIND11_MODULE(_native, module) {
    module.doc() = "Native FFmpeg backend for video_loader";
    av_log_set_level(AV_LOG_ERROR);

    module.def("inspect", [](const py::object& source_object) {
        InputSource source(source_object);
        InspectResult result;
        {
            py::gil_scoped_release release;
            result = inspect_native(source);
        }
        py::dict info;
        info["container"] = "mp4";
        info["codec"] = result.codec;
        info["width"] = result.width;
        info["height"] = result.height;
        info["sample_count"] = result.sample_count;
        info["frame_count"] = result.frame_count;
        info["fps"] = result.fps;
        info["duration_seconds"] = result.duration_seconds;
        info["closure_policy"] = result.closure_policy;
        info["reference_graph_available"] = result.reference_graph_available;
        info["reference_edges"] = result.reference_edges;
        info["has_valid_sample_offsets"] = true;
        return info;
    });

    module.def(
        "decode",
        [](const py::object& source_object, const std::vector<int>& indices) {
            InputSource source(source_object);
            DecodeResult result;
            {
                py::gil_scoped_release release;
                result = decode_native(source, indices);
            }
            py::array_t<uint8_t> output({
                static_cast<py::ssize_t>(result.original_to_unique.size()),
                static_cast<py::ssize_t>(result.height),
                static_cast<py::ssize_t>(result.width),
                static_cast<py::ssize_t>(3),
            });
            py::buffer_info buffer = output.request();
            auto* destination = static_cast<uint8_t*>(buffer.ptr);
            const size_t packed_stride = static_cast<size_t>(result.width) * 3;
            const size_t frame_size = packed_stride
                * static_cast<size_t>(result.height);
            for (size_t output_index = 0;
                 output_index < result.original_to_unique.size();
                 ++output_index) {
                const RgbFrame& frame = result.unique_frames.at(
                    result.original_to_unique[output_index]
                );
                for (int row = 0; row < result.height; ++row) {
                    std::memcpy(
                        destination + output_index * frame_size
                            + static_cast<size_t>(row) * packed_stride,
                        frame.pixels.get()
                            + static_cast<size_t>(row)
                                * static_cast<size_t>(frame.stride),
                        packed_stride
                    );
                }
            }
            return py::make_tuple(std::move(output), decode_info(result));
        }
    );

    module.def(
        "transcode",
        [](const py::array& frames,
           double fps,
           const std::string& mode,
           double crf,
           const std::string& output_path) {
            if (!frames.dtype().is(py::dtype::of<uint8_t>())) {
                throw std::runtime_error("encode: frames dtype must be uint8");
            }
            const py::buffer_info buffer = frames.request();
            if (buffer.ndim != 4 || buffer.shape[3] != 3) {
                throw std::runtime_error(
                    "encode: frames shape must be [T, H, W, 3]"
                );
            }
            ArrayView input;
            input.data = static_cast<const uint8_t*>(buffer.ptr);
            input.frames = buffer.shape[0];
            input.height = buffer.shape[1];
            input.width = buffer.shape[2];
            input.frame_stride = buffer.strides[0];
            input.row_stride = buffer.strides[1];
            input.pixel_stride = buffer.strides[2];
            input.channel_stride = buffer.strides[3];
            {
                py::gil_scoped_release release;
                transcode_native(input, fps, mode, crf, output_path);
            }
        }
    );
}
