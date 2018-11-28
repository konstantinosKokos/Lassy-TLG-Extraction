from abc import ABC, abstractmethod
from functools import reduce
from collections import Counter


class WordType(ABC):
    @abstractmethod
    def __str__(self):
        pass

    @abstractmethod
    def __repr__(self):
        pass

    @abstractmethod
    def __hash__(self):
        pass

    @abstractmethod
    def get_arity(self):
        pass

    @abstractmethod
    def __call__(self):
        pass

    @abstractmethod
    def __eq__(self, other):
        pass

    @abstractmethod
    def decolor(self):
        pass

    @abstractmethod
    def retrieve_atomic(self):
        pass


class AtomicType(WordType):
    def __init__(self, result):
        if not isinstance(result, str):
            raise TypeError('Expected result to be of type str, received {} instead.'.format(type(result)))
        self.result = result

    def __str__(self):
        return self.result

    def __repr__(self):
        return self.__str__()

    def __hash__(self):
        return self.__str__().__hash__()

    def get_arity(self):
        return 0

    def __call__(self):
        return self.__str__()

    def __eq__(self, other):
        if not isinstance(other, AtomicType):
            return False
        else:
            return self.result == other.result

    def decolor(self):
        return self

    def retrieve_atomic(self):
        return {self.__repr__()}


class ModalType(WordType):
    def __init__(self, result, modality):
        if not isinstance(result, WordType):
            raise TypeError('Expected result to be of type WordType, received {} instead.'.format(type(result)))
        self.result = result
        if not isinstance(modality, str):
            raise TypeError('Expected modality to be of type str, received {} instead.'.format(type(modality)))
        self.modality = modality

    def __str__(self):
        if self.result.get_arity():
            return self.modality + '(' + str(self.result) + ')'
        else:
            return self.modality + str(self.result)

    def __repr__(self):
        return self.__str__()

    def __hash__(self):
        return self.__str__().__hash__()

    def get_arity(self):
        return self.result.get_arity()

    def __call__(self):
        return self.__str__()

    def __eq__(self, other):
        if not isinstance(other, ModalType):
            return False
        else:
            return self.modality == other.modality and self.result == other.result

    def decolor(self):
        return ModalType(result=(self.result.decolor()))

    def retrieve_atomic(self):
        return self.result.retrieve_atomic()


class ComplexType(WordType):
    def __init__(self, arguments, result):
        if not isinstance(result, WordType):
            raise TypeError('Expected result to be of type WordType, received {} instead.'.format(type(result)))
        self.result = result
        if not isinstance(arguments, tuple):
            raise TypeError('Expected arguments to be a tuple of WordTypes, received {} instead.'.
                            format(type(arguments)))
        if not all(map(lambda x: isinstance(x, WordType), arguments)) or len(arguments) == 0:
            raise TypeError('Expected arguments to be a non-empty tuple of WordTypes, '
                            'received a tuple containing {} instead.'.format(list(map(type, arguments))))
        self.arguments = sorted(arguments, key=lambda x: x.__repr__())

    def __str__(self):
        if len(self.arguments) > 1:
            return '(' + ', '.join(map(str, self.arguments)) + ') → ' + str(self.result)
        else:
            return str(self.arguments[0]) + ' → ' + str(self.result)

    def __repr__(self):
        return self.__str__()

    def __hash__(self):
        return self.__str__().__hash__()

    def get_arity(self):
        return max(map(lambda x: x.get_arity(), self.arguments)) + 1 + self.result.get_arity()

    def __call__(self):
        return self.__str__()

    def __eq__(self, other):
        if not isinstance(other, ComplexType):
            return False
        else:
            return self.arguments == other.arguments and self.result == other.result

    def decolor(self):
        return ComplexType(arguments=tuple(map(lambda x: x.decolor(), self.arguments)), result=self.result.decolor())

    def retrieve_atomic(self):
        if len(self.arguments) == 1:
            return set.union(self.arguments[0].retrieve_atomic(), self.result.retrieve_atomic())
        else:
            return reduce(set.union, [a.retrieve_atomic() for a in self.arguments])


class ColoredType(ComplexType):
    def __init__(self, arguments, result, colors):
        if not isinstance(colors, tuple):
            raise TypeError('Expected color to be  a tuple of strings, received {} instead.'.format(type(colors)))
        if not all(map(lambda x: isinstance(x, str), colors)) or len(colors) == 0:
            raise TypeError('Expected arguments to be a non-empty tuple of strings,'
                            ' received a tuple containing {} instead.'.format(list(map(type, colors))))
        if len(colors) != len(arguments):
            raise ValueError('Uneven amount of arguments ({}) and colors ({}).'.format(len(arguments), len(colors)))
        sorted_ac = sorted([ac for ac in zip(arguments, colors)], key=lambda x: x[0].__repr__() + x[1].__repr__())
        arguments, colors = list(zip(*sorted_ac))
        super(ColoredType, self).__init__(arguments, result)
        self.colors = colors

    def __str__(self):
        if len(self.arguments) > 1:
            return '(' + ', '.join(map(lambda x: '{' + x[0].__repr__() + ': ' + x[1].__repr__() + '}',
                                       zip(self.arguments, self.colors))) + ') → ' + str(self.result)
        else:
            return '{' + str(self.arguments[0]) + ': ' + self.colors[0] + '} → ' + str(self.result)

    def __repr__(self):
        return self.__str__()

    def __hash__(self):
        return self.__str__().__hash__()

    def __eq__(self, other):
        if not isinstance(other, ColoredType):
            return False
        else:
            return (self.arguments, self.colors) == (other.arguments, other.colors) and self.result == other.result

    def decolor(self):
        return ComplexType(arguments=tuple(map(lambda x: x.decolor(), self.arguments)), result=self.result.decolor())


class CombinatorType(WordType):
    def __init__(self, types, combinator):
        if not isinstance(types, tuple):
            raise TypeError('Expected types to be  a tuple of WordTypes, received {} instead.'.format(type(types)))
        if not all(map(lambda x: isinstance(x, WordType), types)) or len(types) < 1:
            raise TypeError('Expected types to be a non-empty tuple of WordTypes,'
                            ' received a tuple containing {} instead.'.format(list(map(type, types))))
        if not isinstance(combinator, str):
            raise TypeError('Expected combinator to be of type str, received {} instead.'.format(type(combinator)))
        self.types = sorted(types, key=lambda x: x.__repr__())
        self.combinator = combinator

    def __str__(self):
        return (' ' + self.combinator + ' ').join(t.__repr__() for t in self.types)

    def __repr__(self):
        return self.__str__()

    def __hash__(self):
        return self.__str__().__hash__()

    def get_arity(self):
        return max(t.get_arity() for t in self.types)

    def __call__(self):
        return self.__str__()

    def __eq__(self, other):
        if isinstance(other, CombinatorType) and self.types == other.types and self.combinator == other.combinator:
                return True
        return False

    def decolor(self):
        return CombinatorType(types=tuple(map(lambda x: x.decolor(), self.types)), combinator=self.combinator)

    def retrieve_atomic(self):
        return reduce(set.union, [a.retrieve_atomic() for a in self.types])


def compose(base_types, base_colors, result):
    if len(base_types) != len(base_colors):
        raise ValueError('Uneven number of types ({}) and colors ({}).'.format(len(base_types), len(base_colors)))
    return reduce(lambda x, y: ColoredType(result=x, arguments=y[0], colors=y[1]),
                  zip(base_types[::-1], base_colors[::-1]),
                  result)


def decolor(colored_type):
    return colored_type.decolor()


def retrieve_atomic(something):
    if isinstance(something, tuple):
        return reduce(set.union, [retrieve_atomic(s) for s in something])
    else:
        return something.retrieve_atomic()