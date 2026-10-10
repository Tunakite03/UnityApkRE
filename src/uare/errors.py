"""Typed domain errors. Every failure the toolkit raises derives from UareError."""


class UareError(Exception):
    """Base class for all toolkit errors."""


class AdbError(UareError):
    """An adb command failed or returned output that could not be interpreted."""


class DeviceSelectionError(AdbError):
    """No device, or more than one device, matches the requested serial."""


class PackageNotFoundError(AdbError):
    """The package is not installed on the selected device."""


class InvalidPackageNameError(AdbError):
    """The package name is not a valid Android application id."""


class OutputDirectoryError(UareError):
    """The output directory is not empty; the toolkit never overwrites prior results."""


class ApkFormatError(UareError):
    """An APK/OBB archive is malformed, unsafe, or inconsistent with its siblings."""


class UnityFormatError(UareError):
    """A Unity serialized file or bundle header is malformed or unsupported."""


class Il2CppMetadataError(UareError):
    """global-metadata.dat is missing, encrypted, or structurally inconsistent."""


class ElfFormatError(UareError):
    """A native library is not a well-formed ELF file."""
