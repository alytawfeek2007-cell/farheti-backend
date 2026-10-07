import json

from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser

MAX_BODY_BYTES = 64 * 1024


class StrictJSONParser(JSONParser):
    """JSON parser that turns oversized or un-storable bodies into a 400."""

    def parse(self, stream, media_type=None, parser_context=None):
        request = (parser_context or {}).get('request')
        if request is not None:
            try:
                length = int(request.META.get('CONTENT_LENGTH') or 0)
            except (TypeError, ValueError):
                length = 0
            if length > MAX_BODY_BYTES:
                raise ParseError('Request body is too large.')
        try:
            data = super().parse(stream, media_type, parser_context)
            json.dumps(data, ensure_ascii=False).encode('utf-8')
        except RecursionError:
            raise ParseError('JSON is nested too deeply.')
        except UnicodeEncodeError:
            raise ParseError('Invalid characters in request body.')
        return data