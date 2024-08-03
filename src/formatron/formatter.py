import abc
import textwrap
import typing
from copy import copy

import kbnf
from kbnf import AcceptTokenResult, Engine

import grammar_generators.grammar_generator
import schemas.schema
from extractor import Extractor, LiteralExtractor, RegexExtractor, ChoiceExtractor


class FormatterBase(abc.ABC):
    """
    An abstract Formatter that enforces a format on the string generated by a language model.
    """

    @abc.abstractmethod
    def accept_token(self, token_id: int) -> typing.Any:
        """
        Accept a token from the language model.
        :param token_id: The token ID.
        :return: The result of accepting the token.
        """
        pass

    @abc.abstractmethod
    def compute_allowed_tokens(self) -> None:
        """
        Compute the allowed tokens based on the current state.
        """
        pass

    @abc.abstractmethod
    def mask_logits(self, logits) -> typing.Any:
        """
        Mask the logits based on the current state.
        :param logits: The logits to mask.
        :return: The masked logits.
        """
        pass

    @abc.abstractmethod
    def is_completed(self) -> bool:
        """
        Check if the generated string satisfies the format and hence the generation is completed.
        """
        pass

    @abc.abstractmethod
    def on_completion(self, generated_output: str) -> None:
        """
        Perform actions when the generation is completed.
        """
        pass

    @property
    @abc.abstractmethod
    def captures(self) -> dict[str, typing.Any]:
        """
        Get the captures from the generated string.
        """
        pass

    @abc.abstractmethod
    def reset(self) -> None:
        """
        Reset the formatter to the initial state.
        """
        pass


class Formatter(FormatterBase):
    """
    A Formatter that enforces a format on the string generated by a language model.
    """

    def __init__(self, extractors: list[Extractor], engine: kbnf.Engine,
                 decode_callback: typing.Callable[[list[int]], str], grammar_str: str):
        """
        Initialize the formatter.
        :param extractors: The matchers to extract data from the generated string.
        :param engine: The KBNF engine to enforce the format.
        :param decode_callback: The callback to decode the token IDs to a string.
        :param grammar_str: The KBNF grammar string.
        """
        self._extractors = extractors
        self._engine = engine
        self._token_ids = []
        self._decode_callback = decode_callback
        self._grammar_str = grammar_str
        self._captures = {}

    @property
    def grammar_str(self):
        """
        Get the KBNF grammar string.
        """
        return self._grammar_str

    def accept_token(self, token_id: int):
        result = self._engine.try_accept_new_token(token_id)
        self._token_ids.append(token_id)
        if result == AcceptTokenResult.Finished:
            output = self._decode_callback(self._token_ids)
            self.on_completion(output)
        return result

    def accept_bytes(self, _bytes: bytes):
        self._engine.try_accept_new_bytes(_bytes)

    def compute_allowed_tokens(self) -> None:
        self._engine.compute_allowed_token_ids()

    def mask_logits(self, logits) -> typing.Any:
        return self._engine.mask_logits(logits)

    def get_allowed_tokens_since_last_computation(self) -> typing.Sequence[int]:
        return self._engine.get_allowed_token_ids_from_last_computation()

    def get_tokens_to_finish_since_last_computation(self) -> typing.Sequence[int]:
        return self._engine.get_token_ids_to_finish_from_last_computation()

    def is_completed(self) -> bool:
        return self._engine.is_finished()

    def on_completion(self, generated_output: str) -> None:
        for matcher in self._extractors:
            generated_output, captured = matcher.extract(generated_output)
            if matcher.capture_name:
                if matcher.capture_name in self._captures:
                    self._captures[matcher.capture_name] = [self._captures[matcher.capture_name]]
                    self._captures[matcher.capture_name].append(captured)
                else:
                    self._captures[matcher.capture_name] = captured

    @property
    def captures(self) -> dict[str, typing.Any] | None:
        return self._captures

    def reset(self) -> None:
        self._captures.clear()
        self._engine.reset()
        self._token_ids.clear()

    def __str__(self):
        return str(self._engine)


class FormatterBuilder:
    """
    A builder for creating a Formatter.
    """
    _formatter_builder_counter = 0

    def __init__(self):
        """
        Initialize the formatter builder.
        """
        self._counter = 0
        self._main_rule = []
        self._rules = []
        self._capture_names = set()
        self._nonterminal_to_extractor = {}
        self._extractors = []
        self._instance_id = self.__class__._formatter_builder_counter
        self.__class__._formatter_builder_counter += 1

    def _assert_capture_name_valid(self, capture_name: str):
        assert capture_name.isidentifier(), (f"capture_name {capture_name}"
                                             f" should only contains alphanumeric characters, "
                                             f"underscores, and does not start with digits!")
        assert capture_name not in self._capture_names, f"capture_name {capture_name} is duplicated!"

    def append_line(self, line: str) -> None:
        """
        Append a line to the format. Specifically, a newline character is appended to the input.
        """
        self.append_str(line + '\n')

    def append_multiline_str(self, lines: str) -> None:
        """
        Appends a multiline string to the format, preserving the first line's leading whitespaces
        and remove any common leading whitespaces from subsequent lines.

        Note that tabs and spaces are both treated as whitespace, but they are not equal:
        the lines " hello" and "\\thello" are considered to have no common leading whitespace.

        Entirely blank lines are normalized to a newline character.
        """
        first = lines.find('\n')
        self.append_str(lines[:first + 1] + textwrap.dedent(lines[first + 1:]))

    def append_str(self, string: str) -> None:
        """
        Append a string to the format without any post-processing.
        """
        state = "normal"
        last = 0

        def append_literal(end):
            if last < end:
                literal = string[last:end]
                self._main_rule.append(repr(literal))
                self._extractors.append(LiteralExtractor(literal))

        for i, char in enumerate(string):
            if char == "$":
                if state != "escaped":
                    state = "dollar"
                else:
                    state = "normal"
            elif state == "dollar":
                if char == "{":
                    append_literal(i - 1)
                    last = i + 1
                    state = "left_bracket"
                else:
                    state = "normal"
            elif state == "left_bracket":
                if char == "}":
                    state = "normal"
                    self._main_rule.append(string[last:i])
                    self._extractors.append(self._nonterminal_to_extractor[string[last:i]])
                    last = i + 1
            elif char == "\\":
                state = "escaped"
            else:
                state = "normal"
        append_literal(len(string))

    def _create_nonterminal(self, capture_name: typing.Optional[str], name: str) -> str:
        if capture_name is not None:
            self._assert_capture_name_valid(capture_name)
            self._capture_names.add(capture_name)
            nonterminal = f"__{name}_{capture_name}_{self._instance_id}"
        else:
            nonterminal = f"__{name}_{self._counter}_{self._instance_id}"
            self._counter += 1
        return nonterminal

    def choose(self, *extractors: Extractor | str, capture_name: str = None) -> ChoiceExtractor:
        """
        Create a choice extractor.
        :param extractors: The extractors to choose from.
        :param capture_name: The capture name of the extractor, or `None` if the extractor does not capture.
        :return: The choice extractor.
        """
        new_extractors = []
        for extractor in extractors:
            if isinstance(extractor, str):
                new_extractors.append(LiteralExtractor(extractor))
            else:
                new_extractors.append(extractor)
        return self._add_extractor(capture_name, "choice",
                                   lambda nonterminal: ChoiceExtractor(new_extractors, capture_name, nonterminal),
                                   lambda nonterminal:
                                   f"{nonterminal} ::= {' | '.join([i.kbnf_representation for i in new_extractors])};")

    def _add_extractor(self, capture_name: str, extractor_type: str,
                       create_extractor: typing.Callable[[str], Extractor],
                       create_rule: typing.Callable[[str], str]):
        nonterminal = self._create_nonterminal(capture_name, extractor_type)
        self._nonterminal_to_extractor[nonterminal] = create_extractor(nonterminal)
        self._rules.append(create_rule(nonterminal))
        return self._nonterminal_to_extractor[nonterminal]

    def regex(self, regex: str, *, capture_name: str = None) -> RegexExtractor:
        """
        Create a regex extractor.
        :param regex: The regular expression for extraction.
        :param capture_name: The capture name of the extractor, or `None` if the extractor does not capture.
        :return: The regex extractor.
        """
        return self._add_extractor(capture_name, "regex",
                                   lambda nonterminal: RegexExtractor(regex, capture_name, nonterminal),
                                   lambda nonterminal: f"{nonterminal} ::= #{repr(regex)};")

    def schema(self, schema: typing.Type[schemas.schema.Schema],
               grammar_generator: grammar_generators.grammar_generator.GrammarGenerator, *,
               capture_name: str = None) -> Extractor:
        """
        Create a schema extractor.
        :param schema: The schema for extraction.
        :param grammar_generator: The grammar generator to generate the grammar from the schema.
        :param capture_name: The capture name of the extractor, or `None` if the extractor does not capture.
        :return: The schema extractor.
        """
        return self._add_extractor(capture_name, "schema",
                                   lambda nonterminal: grammar_generator.get_extractor(nonterminal, capture_name,
                                                                                       lambda json: schema.from_json(
                                                                                           json)),
                                   lambda nonterminal: grammar_generator.generate(schema, nonterminal))

    def str(self, *, stop: typing.Union[str, list[str]] = None,
            not_contain: typing.Union[str, list[str], None] = None,
            capture_name: typing.Optional[str] = None) -> RegexExtractor:
        """
        Create a string extractor.
        :param stop: The strings for the extractors to stop at. They will be included in text generation and extraction.
        :param not_contain: The strings that should not be included in the generation.
         They will not be included in the generation and extraction.
        :param capture_name: The capture name of the extractor, or `None` if the extractor does not capture.
        :return: The string extractor.
        """
        stop = [stop] if isinstance(stop, str) else stop or []
        not_contain = [not_contain] if isinstance(not_contain, str) else not_contain or []
        nonterminal = self._create_nonterminal(capture_name, "str")
        if not stop and not not_contain:
            capture_regex = ".*"
            nonterminal_regex = "#'.*'"
        else:
            capture_regex = f".*?(?:{'|'.join(map(repr, stop + not_contain))})"
            excepted = f"{nonterminal}_excepted"
            end = f"({'|'.join(map(repr, stop))})" if stop else ""
            nonterminal_regex = f"except!({excepted}){end}"
            self._rules.append(f"{excepted} ::= {' | '.join(map(repr, stop + not_contain))};")
        self._rules.append(f"{nonterminal} ::= {nonterminal_regex};")
        self._nonterminal_to_extractor[nonterminal] = RegexExtractor(capture_regex, capture_name, nonterminal)
        return self._nonterminal_to_extractor[nonterminal]

    def build(self, vocabulary: kbnf.Vocabulary,
              decode: typing.Callable[[list[int]], str],
              engine_config: kbnf.Config = None) -> Formatter:
        """
        Build a formatter from the builder. The builder will not be consumed and can be used again.
        :param vocabulary: The KBNF engine vocabulary for the formatter.
        :param decode: The callback to decode the token IDs to a string.
        :param engine_config: The KBNF engine configuration.
        :return: The formatter.
        """
        assert len(self._main_rule) != 0, "An empty formatter builder cannot build!"
        rules = copy(self._rules)
        rules.append(f"start ::= {' '.join(self._main_rule)};")
        grammar_str = "\n".join(rules)
        engine = Engine(grammar_str, vocabulary, engine_config)
        extractors = copy(self._extractors)
        f = Formatter(extractors, engine, decode, grammar_str)
        return f
