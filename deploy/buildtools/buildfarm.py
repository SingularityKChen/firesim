import logging
import sys
import abc
import pprint

from awstools.awstools import *

# imports needed for python type checking
from typing import cast, Any, Dict, Optional, Sequence, List, TYPE_CHECKING
from mypy_boto3_ec2.service_resource import Instance as EC2InstanceResource
# needed to avoid type-hint circular dependencies
# TODO: Solved in 3.7.+ by "from __future__ import annotations" (see https://stackoverflow.com/questions/33837918/type-hints-solve-circular-dependency)
#       and normal "import <module> as ..." syntax (see https://www.reddit.com/r/Python/comments/cug90e/how_to_not_create_circular_dependencies_when/)
if TYPE_CHECKING:
    from buildtools.buildconfig import BuildConfig
else:
    BuildConfig = object

rootLogger = logging.getLogger()

class BuildHost:
    """Class representing a single basic platform-agnostic build host which holds a single build config.

    Attributes:
        build_config: Build config associated with the build host.
        dest_build_dir: Name of build dir on build host.
        ip_address: IP address of build host.
    """
    build_config: Optional[BuildConfig]
    dest_build_dir: str
    ip_address: str

    def __init__(self, build_config: Optional[BuildConfig] = None, dest_build_dir: str = "", ip_address: str = "") -> None:
        self.build_config = build_config
        self.ip_address = ip_address
        self.dest_build_dir = dest_build_dir

class BuildFarm(metaclass=abc.ABCMeta):
    """Abstract class representing a build farm managing multiple build hosts (request, wait, release, etc).

    Attributes:
        build_hosts: List of build hosts used for builds.
        args: Set of options from the 'args' section of the YAML associated with the build farm.
    """
    build_hosts: List[BuildHost]
    args: Dict[str, Any]

    def __init__(self, args: Dict[str, Any]) -> None:
        """
        Args:
            args: Args (i.e. options) passed to the build farm.
        """
        self.args = args
        self.build_hosts = []

    @staticmethod
    @abc.abstractmethod
    def NAME() -> str:
        """Human-readable name (used as the "build-farm-type" in the YAML).

        Returns:
            Human-readable name.
        """
        return ""

    @abc.abstractmethod
    def request_build_host(self, build_config: BuildConfig) -> None:
        """Request build host to use for build config.

        Args:
            build_config: Build config to request build host for.
        """
        return

    @abc.abstractmethod
    def wait_on_build_host_initialization(self, build_config: BuildConfig) -> None:
        """Ensure build host is launched and ready to be used.

        Args:
            build_config: Build config used to find build host that must ready.
        """
        return

    def get_build_host(self, build_config: BuildConfig) -> BuildHost:
        """Get build host associated with the build config.

        Args:
            build_config: Build config used to find the build host.

        Returns:
            Build host associated with the build config.
        """
        for build_host in self.build_hosts:
            if build_host.build_config == build_config:
                return build_host

        raise Exception(f"Unable to find build host for {build_config.name}")

    def get_build_host_ip(self, build_config: BuildConfig) -> str:
        """Get IP address associated with this dispatched build host.

        Args:
            build_config: Build config to find build host for.

        Returns:
            IP address for the specific build host.
        """
        return self.get_build_host(build_config).ip_address

    @abc.abstractmethod
    def release_build_host(self, build_config: BuildConfig) -> None:
        """Release the build host.

        Args:
            build_config: Build config to find build host to terminate.
        """
        return

class IPAddrBuildFarm(BuildFarm):
    """Build farm that selects from a set of user-determined IPs to allocate a new build host.

    Attributes:
        build_hosts_allocated: Count of build hosts assigned with builds (`BuildConfig`s).
    """
    build_hosts_allocated: int

    def __init__(self, args: Dict[str, Any]) -> None:
        """
        Args:
            args: Args (i.e. options) passed to the build farm.
        """
        super().__init__(args)

        self.parse_args()

    @staticmethod
    def NAME() -> str:
        """Human-readable name (used as the "build-farm-type" in the YAML).

        Returns:
            Human-readable name.
        """
        return "unmanaged"

    def parse_args(self) -> None:
        """Parse build host arguments."""
        self.build_hosts_allocated = 0

        build_farm_hosts_key = "build-farm-hosts"
        build_farm_hosts_list = self.args[build_farm_hosts_key]

        default_build_dir = self.args["default-build-dir"]

        # allocate N build hosts
        for build_farm_host in build_farm_hosts_list:
            if type(build_farm_host) is dict:
                # add element { ip-addr: { arg1: val1, arg2: val2, ... } }

                items = build_farm_host.items()

                assert (len(items) == 1), f"dict type '{build_farm_hosts_key}' items map a single IP address to a dict of options. Not: {pprint.pformat(build_farm_host)}"

                ip_addr, ip_args = next(iter(items))

                dest_build_dir = ip_args.get('override-build-dir', default_build_dir)
            elif type(build_farm_host) is str:
                # add element w/ defaults

                ip_addr = build_farm_host
                dest_build_dir = default_build_dir
            else:
                raise Exception(f"""Unexpected YAML type provided in "{build_farm_hosts_key}" list. Must be dict or str.""")

            if not dest_build_dir:
                raise Exception("ERROR: Invalid null build dir")

            self.build_hosts.append(BuildHost(ip_address=ip_addr, dest_build_dir=dest_build_dir))

    def request_build_host(self, build_config: BuildConfig) -> None:
        """Request build host to use for build config. Just assigns build config to build host since IP address
        is already granted by something outside of FireSim."

        Args:
            build_config: Build config to request build host for.
        """

        if len(self.build_hosts) > self.build_hosts_allocated:
            self.build_hosts[self.build_hosts_allocated].build_config = build_config
            self.build_hosts_allocated += 1
        else:
            bcf = build_config.build_config_file
            error_msg = f"ERROR: {bcf.num_builds} builds requested in `config_build.yaml` but {self.NAME()} build farm only provides {len(self.build_hosts)} build hosts (i.e. IPs)."
            rootLogger.critical(error_msg)
            raise Exception(error_msg)

        return

    def wait_on_build_host_initialization(self, build_config: BuildConfig) -> None:
        """Nothing happens since the provided IP address is already granted by something outside FireSim.

        Args:
            build_config: Build config used to find build host that must ready.
        """
        return

    def release_build_host(self, build_config: BuildConfig) -> None:
        """ Nothing happens. Up to the IP address provider to cleanup after itself.

        Args:
            build_config: Build config to find build host to terminate.
        """
        return

class EC2BuildHost(BuildHost):
    """Class representing an EC2-specific build host instance.

    Attributes:
        launched_instance_object: Boto instance object associated with the build host.
    """
    launched_instance_object: EC2InstanceResource

    def __init__(self, build_config: BuildConfig, inst_obj: EC2InstanceResource) -> None:
        super().__init__(build_config)
        self.launched_instance_object = inst_obj

class EC2BuildFarm(BuildFarm):
    """Build farm to manage AWS EC2 instances as the build hosts.

    Attributes:
        instance_type: instance object type
        build_instance_market: instance market type
        spot_interruption_behavior: if spot instance, the interruption behavior
        spot_max_price: if spot instance, the max price
    """
    instance_type: str
    build_instance_market: str
    spot_interruption_behavior: str
    spot_max_price: str

    def __init__(self, args: Dict[str, Any]) -> None:
        """
        Args:
            args: Args (i.e. options) passed to the build farm.
        """
        super().__init__(args)

        self.parse_args()

    @staticmethod
    def NAME() -> str:
        """Human-readable name (used as the "build-farm-type" in the YAML).

        Returns:
            Human-readable name.
        """
        return "aws-ec2"

    def parse_args(self) -> None:
        """Parse build host arguments."""
        # get aws specific args
        self.instance_type = self.args['instance-type']
        self.build_instance_market = self.args['build-instance-market']
        self.spot_interruption_behavior = self.args['spot-interruption-behavior']
        self.spot_max_price = self.args['spot-max-price']

        self.dest_build_dir = self.args["build-dir"]
        if not self.dest_build_dir:
            raise Exception("ERROR: Invalid null build dir")

    def request_build_host(self, build_config: BuildConfig) -> None:
        """Launch an AWS EC2 instance for the build config.

        Args:
            build_config: Build config to request build host for.
        """

        # get access to the runfarmprefix, which we will apply to build
        # instances too now.
        aws_resource_names_dict = aws_resource_names()
        # just duplicate the runfarmprefix for now. This can be None,
        # in which case we give an empty build farm prefix
        build_farm_prefix = aws_resource_names_dict['runfarmprefix']

        buildfarmprefix = '' if build_farm_prefix is None else build_farm_prefix

        inst_obj = launch_instances(
            self.instance_type,
            1,
            self.build_instance_market,
            self.spot_interruption_behavior,
            self.spot_max_price,
            blockdevices=[
                {
                    'DeviceName': '/dev/sda1',
                    'Ebs': {
                        'VolumeSize': 200,
                        'VolumeType': 'gp2',
                    },
                },
            ],
            tags={ 'fsimbuildcluster': buildfarmprefix },
            randomsubnet=True)[0]

        self.build_hosts.append(EC2BuildHost(build_config, inst_obj))

    def wait_on_build_host_initialization(self, build_config: BuildConfig) -> None:
        """Wait for EC2 instance launch.

        Args:
            build_config: Build config used to find build host that must ready.
        """
        build_host = cast(EC2BuildHost, self.get_build_host(build_config))
        wait_on_instance_launches([build_host.launched_instance_object])
        build_host.ip_address = build_host.launched_instance_object.private_ip_address

    def release_build_host(self, build_config: BuildConfig) -> None:
        """ Terminate the EC2 instance running this build.

        Args:
            build_config: Build config to find build host to terminate.
        """
        build_host = cast(EC2BuildHost, self.get_build_host(build_config))
        instance_ids = get_instance_ids_for_instances([build_host.launched_instance_object])
        rootLogger.info(f"Terminating build instance {instance_ids}. Please confirm in your AWS Management Console")
        terminate_instances(instance_ids, dryrun=False)
