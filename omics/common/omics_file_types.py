from enum import Enum
from typing import Any, List, Type


class ExtendedEnum(Enum):
    """Enum subclass that includes helper methods."""

    @classmethod
    def list(cls: Type[Enum]) -> List[str]:
        """Return the list of allowed values in an Enum."""
        return list(map(lambda c: c.value, cls))  # type: ignore

    @classmethod
    def from_object(cls: Type[Enum], object: Any) -> Enum:
        """Convert an object to the Enum type if possible.

        Since Python allows strings to be passed as parameters even when an Enum
        type is specified, this method converts a string to an enum if it
        matches one of the allowed values.

        Args:
            object: the object to convert to an Enum.
        """
        if isinstance(object, cls):
            return object
        if isinstance(object, str):
            for enum in cls:
                if object.upper() == enum.value:
                    return enum
            valid_values = ", ".join(cls.list())
            raise AttributeError(f"{cls.__name__} must be one of {valid_values}")

        raise AttributeError(f"Unsupported type for {cls.__name__}: {type(object)}")


class ReadSetFileName(ExtendedEnum):
    """Available read set file names."""

    SOURCE1 = "SOURCE1"
    SOURCE2 = "SOURCE2"
    INDEX = "INDEX"


class ReferenceFileName(ExtendedEnum):
    """Available reference file names."""

    SOURCE = "SOURCE"
    INDEX = "INDEX"


class OmicsFileType(ExtendedEnum):
    """Available file types."""

    READSET = "READSET"
    REFERENCE = "REFERENCE"


OMICS_URI_TYPE_FILENAME_MAP = {
    OmicsFileType.READSET: ReadSetFileName,
    OmicsFileType.REFERENCE: ReferenceFileName,
}


OMICS_URI_TYPE_DEFAULT_FILENAME_MAP = {
    OmicsFileType.READSET: ReadSetFileName.SOURCE1,
    OmicsFileType.REFERENCE: ReferenceFileName.SOURCE,
}


class ReadSetFileType(ExtendedEnum):
    """Available read set file types."""

    FASTQ = "FASTQ"
    BAM = "BAM"
    CRAM = "CRAM"
    UBAM = "UBAM"
