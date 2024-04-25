import json
import io


class ParseError(Exception):
    pass


class NewlineFinder:
    error_for_eof = None

    def process(self, output_stream, chunk):
        index = chunk.find(b"\n")
        if index != -1:
            part_of_result, remainder = chunk[:index], chunk[index + 1:]
            output_stream.write(part_of_result)
            return True, remainder

        output_stream.write(chunk)
        return False, b""


class LengthFinder:

    def __init__(self, length, error_for_eof):
        self.error_for_eof = error_for_eof
        self.length = length
        self.count = 0

    def process(self, output_stream, chunk):
        needed = self.length - self.count
        if needed <= len(chunk):
            part_of_result, remainder = chunk[:needed], chunk[needed:]
            output_stream.write(part_of_result)
            return True, remainder

        self.count += len(chunk)
        output_stream.write(chunk)
        return False, b""


def readuntil(input_stream, initial_chunk, finder, output_stream, chunk_size):
    chunk = initial_chunk
    done = False

    done, remainder = finder.process(output_stream, chunk)

    while not done:
        chunk = input_stream.read(chunk_size)

        if not chunk:
            if finder.error_for_eof is None:
                # eof is implicit success
                return b"", True

            raise ParseError(finder.error_for_eof)

        done, remainder = finder.process(output_stream, chunk)

    return remainder, False


class StreamingEnvelopeParser:

    def __init__(self, input_stream, chunk_size=1024):
        self.input_stream = input_stream
        self.chunk_size = chunk_size

        self.remainder = b""  # leftover from previous read chunk that's not handled by a parser yet
        self.at_eof = False

        self.envelope_headers = None

    def _parse_headers(self, eof_is_error):
        """
        Quoted from https://develop.sentry.dev/sdk/envelopes/#headers at version 9c7f19f96562
        conversion to numbered list mine

        > ### Headers

        > Envelopes contain Headers in several places. Headers are JSON-encoded objects
        > (key-value mappings) that follow these rules:

        > 1. Always encoded in UTF-8
        > 2. Must be valid JSON
        > 3. Must be declared in a single line; no newlines
        > 4. Always followed by a newline (`\n`) or the end of the file
        > 5. Must not be padded by leading or trailing whitespace
        > 6. Should be serialized in their most compact form without additional white
        >    space. Whitespace within the JSON headers is permitted, though discouraged.
        > 7. Unknown attributes are allowed and should be retained by all implementations;
        >    however, attributes not covered in this spec must not be actively emitted by
        >    any implementation.
        > 8. All known headers and their data types can be validated by an implementation;
        >    if validation fails, the Envelope may be rejected as malformed.
        > 9. Empty headers `{}` are technically valid

        (Note that the combination of point 6 and the fact that JSON strings may not contain newlines unescaped makes
        the whole headers-terminated-by-newline possible)
        """

        header_stream = io.BytesIO()

        # points 3, 4 (we don't use 5, 6, 7, 9 explicitly)
        self.remainder, self.at_eof = readuntil(
            self.input_stream, self.remainder, NewlineFinder(), header_stream, self.chunk_size)

        header_stream_value = header_stream.getvalue()
        if self.at_eof:
            if header_stream_value == b"":
                return None

            if eof_is_error:
                # We found some header-data, but nothing else. This is an error
                raise ParseError("EOF when reading headers; what is this a header for then?")

        try:
            return json.loads(header_stream_value.decode("utf-8"))  # points 1, 2
        except json.JSONDecodeError as e:
            raise ParseError("Header not JSON") from e

    def get_envelope_headers(self):
        if self.envelope_headers is None:
            self.envelope_headers = self._parse_headers(eof_is_error=False)
            assert self.envelope_headers is not None

        return self.envelope_headers

    def get_items(self, output_stream_factory):
        # yields the item_headers and item_output_streams (with the content of the items written into them)
        # closing the item_output_stream is the responsibility of the calller

        self.get_envelope_headers()

        while not self.at_eof:
            item_headers = self._parse_headers(eof_is_error=True)
            if item_headers is None:
                self.at_eof = True
                break

            if "length" in item_headers:
                length = item_headers["length"]
                finder = LengthFinder(length, error_for_eof="EOF while reading item with explicitly specified length")
            else:
                finder = NewlineFinder()

            item_output_stream = output_stream_factory(item_headers)
            self.remainder, self.at_eof = readuntil(
                self.input_stream, self.remainder, finder, item_output_stream, self.chunk_size)

            if "length" in item_headers:
                # items with an explicit length are terminated by a newline (if at EOF, this is optional as per the set
                # of examples in the docs)
                self.remainder, self.at_eof = readuntil(
                    self.input_stream, self.remainder, NewlineFinder(), io.BytesIO(), self.chunk_size)

            yield item_headers, item_output_stream

    def get_items_directly(self):
        # this method is just convenience for testing

        for item_headers, output_stream in self.get_items(lambda item_headers: io.BytesIO()):
            yield item_headers, output_stream.getvalue()
