import functools
import json
import os
import time

import luigi

from servicecatalog_puppet import aws
from servicecatalog_puppet import config
from servicecatalog_puppet import constants
from servicecatalog_puppet.workflow import (
    tasks,
    portfoliomanagement as portfoliomanagement_tasks,
    manifest as manifest_tasks,
    dependency,
    generate,
)


class LaunchSectionTask(manifest_tasks.SectionTask):
    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "cache_invalidator": self.cache_invalidator,
        }

    def requires(self):
        self.info(f"Launching and execution mode is: {self.execution_mode}")
        requirements = list()

        for name, details in self.manifest.get(constants.LAUNCHES, {}).items():
            execution = details.get("execution")

            if self.is_running_in_spoke():
                if execution == constants.EXECUTION_MODE_SPOKE:
                    requirements += self.handle_requirements_for(
                        name,
                        constants.LAUNCH,
                        constants.LAUNCHES,
                        LaunchForRegionTask,
                        LaunchForAccountTask,
                        LaunchForAccountAndRegionTask,
                        LaunchTask,
                        dict(
                            launch_name=name,
                            puppet_account_id=self.puppet_account_id,
                            manifest_file_path=self.manifest_file_path,
                        ),
                    )
                else:
                    continue

            else:
                if execution != constants.EXECUTION_MODE_SPOKE:
                    requirements += self.handle_requirements_for(
                        name,
                        constants.LAUNCH,
                        constants.LAUNCHES,
                        LaunchForRegionTask,
                        LaunchForAccountTask,
                        LaunchForAccountAndRegionTask,
                        LaunchTask,
                        dict(
                            launch_name=name,
                            puppet_account_id=self.puppet_account_id,
                            manifest_file_path=self.manifest_file_path,
                        ),
                    )
                else:
                    requirements.append(
                        LaunchForSpokeExecutionTask(
                            launch_name=name,
                            puppet_account_id=self.puppet_account_id,
                            manifest_file_path=self.manifest_file_path,
                        )
                    )

        return requirements

    def run(self):
        self.write_output(self.manifest.get("launches", {}))


class ProvisioningTask(tasks.PuppetTaskWithParameters, manifest_tasks.ManifestMixen):
    manifest_file_path = luigi.Parameter()

    @property
    def status(self):
        return (
            self.manifest.get(constants.LAUNCHES)
            .get(self.launch_name)
            .get("status", constants.PROVISIONED)
        )

    @property
    def section_name(self):
        return constants.LAUNCHES


class ListLaunchPathsTask(ProvisioningTask):
    puppet_account_id = luigi.Parameter()
    portfolio = luigi.Parameter()
    product_id = luigi.Parameter()
    account_id = luigi.Parameter()
    region = luigi.Parameter()

    def api_calls_used(self):
        return [
            f"servicecatalog.list_launch_paths_{self.account_id}_{self.region}",
        ]

    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "portfolio": self.portfolio,
            "region": self.region,
            "product_id": self.product_id,
            "account_id": self.account_id,
            "cache_invalidator": self.cache_invalidator,
        }

    def run(self):
        with self.hub_regional_client("servicecatalog") as service_catalog:
            self.info(f"Getting path for product {self.product_id}")
            response = service_catalog.list_launch_paths(ProductId=self.product_id)
            if len(response.get("LaunchPathSummaries")) == 1:
                path_id = response.get("LaunchPathSummaries")[0].get("Id")
                self.info(
                    f"There is only one path: {path_id} for product: {self.product_id}"
                )
                self.write_output(response.get("LaunchPathSummaries")[0])
            else:
                for launch_path_summary in response.get("LaunchPathSummaries", []):
                    name = launch_path_summary.get("Name")
                    if name == self.portfolio:
                        path_id = launch_path_summary.get("Id")
                        self.info(f"Got path: {path_id} for product: {self.product_id}")
                        self.write_output(launch_path_summary)
        raise Exception("Could not find a launch path")


class ProvisioningArtifactParametersTask(ProvisioningTask):
    puppet_account_id = luigi.Parameter()
    portfolio = luigi.Parameter()
    product = luigi.Parameter()
    version = luigi.Parameter()
    region = luigi.Parameter()

    @property
    def retry_count(self):
        return 5

    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "portfolio": self.portfolio,
            "region": self.region,
            "product": self.product,
            "version": self.version,
            "cache_invalidator": self.cache_invalidator,
        }

    def requires(self):
        required = dict(
            details=portfoliomanagement_tasks.GetVersionDetailsByNames(
                manifest_file_path=self.manifest_file_path,
                puppet_account_id=self.puppet_account_id,
                portfolio=self.portfolio,
                product=self.product,
                version=self.version,
                account_id=self.single_account
                if self.execution_mode == constants.EXECUTION_MODE_SPOKE
                else self.puppet_account_id,
                region=self.region,
            ),
        )
        if self.execution_mode != constants.EXECUTION_MODE_SPOKE:
            required[
                "associations"
            ] = portfoliomanagement_tasks.CreateAssociationsInPythonForPortfolioTask(
                manifest_file_path=self.manifest_file_path,
                puppet_account_id=self.puppet_account_id,
                account_id=self.puppet_account_id,
                region=self.region,
                portfolio=self.portfolio,
            )
        return required

    def run(self):
        details = self.load_from_input("details")
        product_id = details.get("product_details").get("ProductId")
        version_id = details.get("version_details").get("Id")
        result = yield DoDescribeProvisioningParameters(
            manifest_file_path=self.manifest_file_path,
            puppet_account_id=self.single_account
            if self.execution_mode == constants.EXECUTION_MODE_SPOKE
            else self.puppet_account_id,
            region=self.region,
            product_id=product_id,
            version_id=version_id,
            portfolio=self.portfolio,
        )
        self.write_output(
            result.open("r").read(), skip_json_dump=True,
        )


class DoDescribeProvisioningParameters(ProvisioningTask):
    puppet_account_id = luigi.Parameter()
    region = luigi.Parameter()
    product_id = luigi.Parameter()
    version_id = luigi.Parameter()
    portfolio = luigi.Parameter()

    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "portfolio": self.portfolio,
            "region": self.region,
            "product_id": self.product_id,
            "version_id": self.version_id,
        }

    def api_calls_used(self):
        return [
            f"servicecatalog.describe_provisioning_parameters_{self.puppet_account_id}_{self.region}",
        ]

    def run(self):
        with self.hub_regional_client("servicecatalog") as service_catalog:

            provisioning_artifact_parameters = None
            retries = 3
            while retries > 0:
                try:
                    provisioning_artifact_parameters = service_catalog.describe_provisioning_parameters(
                        ProductId=self.product_id,
                        ProvisioningArtifactId=self.version_id,
                        PathName=self.portfolio,
                    ).get(
                        "ProvisioningArtifactParameters", []
                    )
                    retries = 0
                    break
                except service_catalog.exceptions.ClientError as ex:
                    if "S3 error: Access Denied" in str(ex):
                        self.info("Swallowing S3 error: Access Denied")
                    else:
                        raise ex
                    time.sleep(3)
                    retries -= 1

            self.write_output(
                provisioning_artifact_parameters
                if isinstance(provisioning_artifact_parameters, list)
                else [provisioning_artifact_parameters]
            )


class ProvisionProductTask(ProvisioningTask, dependency.DependenciesMixin):
    launch_name = luigi.Parameter()
    puppet_account_id = luigi.Parameter()

    region = luigi.Parameter()
    account_id = luigi.Parameter()

    portfolio = luigi.Parameter()
    product = luigi.Parameter()
    version = luigi.Parameter()

    ssm_param_inputs = luigi.ListParameter(default=[], significant=False)

    launch_parameters = luigi.DictParameter(default={}, significant=False)
    manifest_parameters = luigi.DictParameter(default={}, significant=False)
    account_parameters = luigi.DictParameter(default={}, significant=False)

    retry_count = luigi.IntParameter(default=1, significant=False)
    worker_timeout = luigi.IntParameter(default=0, significant=False)
    ssm_param_outputs = luigi.ListParameter(default=[], significant=False)
    requested_priority = luigi.IntParameter(significant=False, default=0)

    execution = luigi.Parameter()

    try_count = 1

    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "launch_name": self.launch_name,
            "account_id": self.account_id,
            "region": self.region,
            "cache_invalidator": self.cache_invalidator,
        }

    @property
    def priority(self):
        return self.requested_priority

    def requires(self):
        requirements = {
            "section_dependencies": self.get_section_dependencies(),
            "ssm_params": self.get_ssm_parameters(),
            "provisioning_artifact_parameters": ProvisioningArtifactParametersTask(
                manifest_file_path=self.manifest_file_path,
                puppet_account_id=self.puppet_account_id,
                portfolio=self.portfolio,
                product=self.product,
                version=self.version,
                region=self.region,
            ),
            "details": portfoliomanagement_tasks.GetVersionDetailsByNames(
                manifest_file_path=self.manifest_file_path,
                puppet_account_id=self.puppet_account_id,
                account_id=self.single_account
                if self.execution_mode == constants.EXECUTION_MODE_SPOKE
                else self.puppet_account_id,
                portfolio=self.portfolio,
                product=self.product,
                version=self.version,
                region=self.region,
            ),
        }
        return requirements

    def api_calls_used(self):
        apis = [
            f"servicecatalog.scan_provisioned_products_single_page_{self.account_id}_{self.region}",
            f"servicecatalog.describe_provisioned_product_{self.account_id}_{self.region}",
            f"servicecatalog.terminate_provisioned_product_{self.account_id}_{self.region}",
            f"servicecatalog.describe_record_{self.account_id}_{self.region}",
            f"cloudformation.get_template_summary_{self.account_id}_{self.region}",
            f"cloudformation.describe_stacks_{self.account_id}_{self.region}",
            f"servicecatalog.list_provisioned_product_plans_single_page_{self.account_id}_{self.region}",
            f"servicecatalog.delete_provisioned_product_plan_{self.account_id}_{self.region}",
            f"servicecatalog.create_provisioned_product_plan_{self.account_id}_{self.region}",
            f"servicecatalog.describe_provisioned_product_plan_{self.account_id}_{self.region}",
            f"servicecatalog.execute_provisioned_product_plan_{self.account_id}_{self.region}",
            f"servicecatalog.describe_provisioned_product_{self.account_id}_{self.region}",
            f"servicecatalog.update_provisioned_product_{self.account_id}_{self.region}",
            f"servicecatalog.provision_product_{self.account_id}_{self.region}",
            # f"ssm.put_parameter_and_wait_{self.region}",
        ]
        if self.should_use_product_plans:
            apis.append(
                f"servicecatalog.list_launch_paths_{self.account_id}_{self.region}",
            )
        return apis

    def run(self):
        details = self.load_from_input("details")
        product_id = details.get("product_details").get("ProductId")
        version_id = details.get("version_details").get("Id")

        task_output = dict(
            **self.params_for_results_display(),
            account_parameters=tasks.unwrap(self.account_parameters),
            launch_parameters=tasks.unwrap(self.launch_parameters),
            manifest_parameters=tasks.unwrap(self.manifest_parameters),
        )

        all_params = self.get_parameter_values()

        with self.spoke_regional_client("servicecatalog") as service_catalog:
            path_name = self.portfolio

            (
                provisioned_product_id,
                provisioning_artifact_id,
                provisioned_product_status,
            ) = aws.terminate_if_status_is_not_available(
                service_catalog,
                self.launch_name,
                product_id,
                self.account_id,
                self.region,
            )
            self.info(
                f"pp_id: {provisioned_product_id}, paid : {provisioning_artifact_id}"
            )

            with self.spoke_regional_client("cloudformation") as cloudformation:
                need_to_provision = True

                self.info(
                    f"running ,checking {product_id} {version_id} {path_name} in {self.account_id} {self.region}"
                )

                with self.input().get("provisioning_artifact_parameters").open(
                    "r"
                ) as f:
                    provisioning_artifact_parameters = json.loads(f.read())

                params_to_use = {}
                for p in provisioning_artifact_parameters:
                    param_name = p.get("ParameterKey")
                    params_to_use[param_name] = all_params.get(
                        param_name, p.get("DefaultValue")
                    )

                if provisioning_artifact_id == version_id:
                    self.info(f"found previous good provision")
                    if provisioned_product_id:
                        self.info(f"checking params for diffs")
                        provisioned_parameters = aws.get_parameters_for_stack(
                            cloudformation,
                            f"SC-{self.account_id}-{provisioned_product_id}",
                        )
                        self.info(f"current params: {provisioned_parameters}")

                        self.info(f"new params: {params_to_use}")

                        if provisioned_parameters == params_to_use:
                            self.info(f"params unchanged")
                            need_to_provision = False
                        else:
                            self.info(f"params changed")

                if provisioned_product_status == "TAINTED":
                    need_to_provision = True

                if need_to_provision:
                    self.info(
                        f"about to provision with params: {json.dumps(tasks.unwrap(params_to_use))}"
                    )

                    if provisioned_product_id:
                        stack = aws.get_stack_output_for(
                            cloudformation,
                            f"SC-{self.account_id}-{provisioned_product_id}",
                        )
                        stack_status = stack.get("StackStatus")
                        self.info(f"current cfn stack_status is {stack_status}")
                        if stack_status not in [
                            "UPDATE_COMPLETE",
                            "CREATE_COMPLETE",
                            "UPDATE_ROLLBACK_COMPLETE",
                        ]:
                            raise Exception(
                                f"[{self.uid}] current cfn stack_status is {stack_status}"
                            )
                        if stack_status == "UPDATE_ROLLBACK_COMPLETE":
                            self.warning(
                                f"[{self.uid}] SC-{self.account_id}-{provisioned_product_id} has a status of "
                                f"{stack_status}.  This may need manual resolution."
                            )

                    if provisioned_product_id:
                        if self.should_use_product_plans:
                            path_id = aws.get_path_for_product(
                                service_catalog, product_id, self.portfolio
                            )
                            provisioned_product_id = aws.provision_product_with_plan(
                                service_catalog,
                                self.launch_name,
                                self.account_id,
                                self.region,
                                product_id,
                                version_id,
                                self.puppet_account_id,
                                path_id,
                                params_to_use,
                                self.version,
                                self.should_use_sns,
                            )
                        else:
                            provisioned_product_id = aws.update_provisioned_product(
                                service_catalog,
                                self.launch_name,
                                self.account_id,
                                self.region,
                                product_id,
                                version_id,
                                self.puppet_account_id,
                                path_name,
                                params_to_use,
                                self.version,
                                self.execution,
                            )

                    else:
                        provisioned_product_id = aws.provision_product(
                            service_catalog,
                            self.launch_name,
                            self.account_id,
                            self.region,
                            product_id,
                            version_id,
                            self.puppet_account_id,
                            path_name,
                            params_to_use,
                            self.version,
                            self.should_use_sns,
                            self.execution,
                        )

                self.info(f"self.execution is {self.execution}")
                if self.execution == constants.EXECUTION_MODE_HUB:
                    self.info(
                        f"Running in execution mode: {self.execution}, checking for SSM outputs"
                    )
                    with self.spoke_regional_client(
                        "cloudformation"
                    ) as spoke_cloudformation:
                        stack_details = aws.get_stack_output_for(
                            spoke_cloudformation,
                            f"SC-{self.account_id}-{provisioned_product_id}",
                        )

                    for ssm_param_output in self.ssm_param_outputs:
                        self.info(
                            f"writing SSM Param: {ssm_param_output.get('stack_output')}"
                        )
                        with self.hub_client("ssm") as ssm:
                            found_match = False
                            # TODO push into another task
                            for output in stack_details.get("Outputs", []):
                                if output.get("OutputKey") == ssm_param_output.get(
                                    "stack_output"
                                ):
                                    ssm_parameter_name = ssm_param_output.get(
                                        "param_name"
                                    )
                                    ssm_parameter_name = ssm_parameter_name.replace(
                                        "${AWS::Region}", self.region
                                    )
                                    ssm_parameter_name = ssm_parameter_name.replace(
                                        "${AWS::AccountId}", self.account_id
                                    )
                                    found_match = True
                                    self.info(f"found value")
                                    ssm.put_parameter_and_wait(
                                        Name=ssm_parameter_name,
                                        Value=output.get("OutputValue"),
                                        Type=ssm_param_output.get(
                                            "param_type", "String"
                                        ),
                                        Overwrite=True,
                                    )
                            if not found_match:
                                raise Exception(
                                    f"[{self.uid}] Could not find match for {ssm_param_output.get('stack_output')}"
                                )

                    self.write_output(task_output)
                else:
                    self.write_output(task_output)
                self.info("finished")


class ProvisionProductDryRunTask(ProvisionProductTask):
    def output(self):
        return luigi.LocalTarget(self.output_location)

    @property
    def output_location(self):
        return f"output/{self.uid}.{self.output_suffix}"

    def api_calls_used(self):
        return [
            f"servicecatalog.scan_provisioned_products_single_page_{self.account_id}_{self.region}",
            f"servicecatalog.list_launch_paths_{self.account_id}_{self.region}",
            f"servicecatalog.describe_provisioning_artifact_{self.account_id}_{self.region}",
            f"cloudformation.describe_provisioning_artifact_{self.account_id}_{self.region}",
            f"cloudformation.get_template_summary_{self.account_id}_{self.region}",
            f"cloudformation.describe_stacks_{self.account_id}_{self.region}",
        ]

    def run(self):
        details = self.load_from_input("details")
        product_id = details.get("product_details").get("ProductId")
        version_id = details.get("version_details").get("Id")

        self.info(f"starting deploy try {self.try_count} of {self.retry_count}")

        all_params = self.get_parameter_values()
        with self.spoke_regional_client("servicecatalog") as service_catalog:
            self.info(f"looking for previous failures")
            path_name = self.portfolio

            response = service_catalog.scan_provisioned_products_single_page(
                AccessLevelFilter={"Key": "Account", "Value": "self"},
            )

            provisioned_product_id = False
            provisioning_artifact_id = None
            current_status = "NOT_PROVISIONED"
            for r in response.get("ProvisionedProducts", []):
                if r.get("Name") == self.launch_name:
                    current_status = r.get("Status")
                    if current_status in ["AVAILABLE", "TAINTED"]:
                        provisioned_product_id = r.get("Id")
                        provisioning_artifact_id = r.get("ProvisioningArtifactId")

            if provisioning_artifact_id is None:
                self.info(f"params unchanged")
                self.write_result(
                    current_version="-",
                    new_version=self.version,
                    effect=constants.CHANGE,
                    current_status="NOT_PROVISIONED",
                    active="N/A",
                    notes="New provisioning",
                )
            else:
                self.info(
                    f"pp_id: {provisioned_product_id}, paid : {provisioning_artifact_id}"
                )
                current_version_details = self.get_current_version(
                    provisioning_artifact_id, product_id, service_catalog
                )

                with self.spoke_regional_client("cloudformation") as cloudformation:
                    self.info(
                        f"checking {product_id} {version_id} {path_name} in {self.account_id} {self.region}"
                    )

                    with self.input().get("provisioning_artifact_parameters").open(
                        "r"
                    ) as f:
                        provisioning_artifact_parameters = json.loads(f.read())

                    params_to_use = {}
                    for p in provisioning_artifact_parameters:
                        param_name = p.get("ParameterKey")
                        params_to_use[param_name] = all_params.get(
                            param_name, p.get("DefaultValue")
                        )

                    if provisioning_artifact_id == version_id:
                        self.info(f"found previous good provision")
                        if provisioned_product_id:
                            self.info(f"checking params for diffs")
                            provisioned_parameters = aws.get_parameters_for_stack(
                                cloudformation,
                                f"SC-{self.account_id}-{provisioned_product_id}",
                            )
                            self.info(f"current params: {provisioned_parameters}")
                            self.info(f"new params: {params_to_use}")

                            if provisioned_parameters == params_to_use:
                                self.info(f"params unchanged")
                                self.write_result(
                                    current_version=self.version,
                                    new_version=self.version,
                                    effect=constants.NO_CHANGE,
                                    current_status=current_status,
                                    active=current_version_details.get("Active"),
                                    notes="Versions and params are the same",
                                )
                            else:
                                self.write_result(
                                    current_version=self.version,
                                    new_version=self.version,
                                    effect=constants.CHANGE,
                                    current_status=current_status,
                                    active=current_version_details.get("Active"),
                                    notes="Versions are the same but the params are different",
                                )
                    else:
                        if provisioning_artifact_id:
                            current_version = current_version_details.get("Name")
                            active = current_version_details.get("Active")
                        else:
                            current_version = ""
                            active = False
                        self.write_result(
                            current_version=current_version,
                            new_version=self.version,
                            effect=constants.CHANGE,
                            current_status=current_status,
                            active=active,
                            notes="Version change",
                        )

    def get_current_version(
        self, provisioning_artifact_id, product_id, service_catalog
    ):
        return service_catalog.describe_provisioning_artifact(
            ProvisioningArtifactId=provisioning_artifact_id, ProductId=product_id,
        ).get("ProvisioningArtifactDetail")

    def write_result(
        self, current_version, new_version, effect, current_status, active, notes=""
    ):
        with self.output().open("w") as f:
            f.write(
                json.dumps(
                    {
                        "current_version": current_version,
                        "new_version": new_version,
                        "effect": effect,
                        "current_status": current_status,
                        "active": active,
                        "notes": notes,
                        "params": self.param_kwargs,
                    },
                    indent=4,
                    default=str,
                )
            )


class TerminateProductTask(ProvisioningTask, dependency.DependenciesMixin):
    launch_name = luigi.Parameter()
    puppet_account_id = luigi.Parameter()

    region = luigi.Parameter()
    account_id = luigi.Parameter()

    portfolio = luigi.Parameter()
    product = luigi.Parameter()
    version = luigi.Parameter()

    ssm_param_inputs = luigi.ListParameter(default=[], significant=False)

    launch_parameters = luigi.DictParameter(default={}, significant=False)
    manifest_parameters = luigi.DictParameter(default={}, significant=False)
    account_parameters = luigi.DictParameter(default={}, significant=False)

    retry_count = luigi.IntParameter(default=1, significant=False)
    worker_timeout = luigi.IntParameter(default=0, significant=False)
    ssm_param_outputs = luigi.ListParameter(default=[], significant=False)
    requested_priority = luigi.IntParameter(significant=False, default=0)

    execution = luigi.Parameter()

    try_count = 1

    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "launch_name": self.launch_name,
            "account_id": self.account_id,
            "region": self.region,
            "cache_invalidator": self.cache_invalidator,
        }

    def requires(self):
        requirements = {"section_dependencies": self.get_section_dependencies()}

        return requirements

    def run(self):
        yield DoTerminateProductTask(
            manifest_file_path=self.manifest_file_path,
            launch_name=self.launch_name,
            puppet_account_id=self.puppet_account_id,
            region=self.region,
            account_id=self.account_id,
            portfolio=self.portfolio,
            product=self.product,
            version=self.version,
            ssm_param_inputs=self.ssm_param_inputs,
            launch_parameters=self.launch_parameters,
            manifest_parameters=self.manifest_parameters,
            account_parameters=self.account_parameters,
            retry_count=self.retry_count,
            worker_timeout=self.worker_timeout,
            ssm_param_outputs=self.ssm_param_outputs,
            requested_priority=self.requested_priority,
            execution=self.execution,
        )
        self.write_output(self.params_for_results_display())


class DoTerminateProductTask(ProvisioningTask, dependency.DependenciesMixin):
    launch_name = luigi.Parameter()
    puppet_account_id = luigi.Parameter()

    region = luigi.Parameter()
    account_id = luigi.Parameter()

    portfolio = luigi.Parameter()
    product = luigi.Parameter()
    version = luigi.Parameter()

    ssm_param_inputs = luigi.ListParameter(default=[], significant=False)

    launch_parameters = luigi.DictParameter(default={}, significant=False)
    manifest_parameters = luigi.DictParameter(default={}, significant=False)
    account_parameters = luigi.DictParameter(default={}, significant=False)

    retry_count = luigi.IntParameter(default=1, significant=False)
    worker_timeout = luigi.IntParameter(default=0, significant=False)
    ssm_param_outputs = luigi.ListParameter(default=[], significant=False)
    requested_priority = luigi.IntParameter(significant=False, default=0)

    execution = luigi.Parameter()

    try_count = 1

    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "launch_name": self.launch_name,
            "account_id": self.account_id,
            "region": self.region,
            "cache_invalidator": self.cache_invalidator,
        }

    def requires(self):
        requirements = {
            "details": portfoliomanagement_tasks.GetVersionDetailsByNames(
                manifest_file_path=self.manifest_file_path,
                puppet_account_id=self.puppet_account_id,
                account_id=self.single_account
                if self.execution_mode == constants.EXECUTION_MODE_SPOKE
                else self.puppet_account_id,
                portfolio=self.portfolio,
                product=self.product,
                version=self.version,
                region=self.region,
            ),
        }

        return requirements

    def api_calls_used(self):
        return [
            f"servicecatalog.scan_provisioned_products_single_page{self.account_id}_{self.region}",
            f"servicecatalog.terminate_provisioned_product_{self.account_id}_{self.region}",
            f"servicecatalog.describe_record_{self.account_id}_{self.region}",
            # f"ssm.delete_parameter_{self.region}": 1,
        ]

    def run(self):
        self.info(f"starting terminate try {self.try_count} of {self.retry_count}")
        details = self.load_from_input("details")
        product_id = details.get("product_details").get("ProductId")

        with self.spoke_regional_client("servicecatalog") as service_catalog:
            self.info(
                f"[{self.launch_name}] {self.account_id}:{self.region} :: looking for previous failures"
            )
            provisioned_product_id, provisioning_artifact_id = aws.ensure_is_terminated(
                service_catalog, self.launch_name, product_id
            )
            log_output = self.to_str_params()
            log_output.update(
                {"provisioned_product_id": provisioned_product_id,}
            )

            for ssm_param_output in self.ssm_param_outputs:
                param_name = ssm_param_output.get("param_name")
                self.info(
                    f"[{self.launch_name}] {self.account_id}:{self.region} :: deleting SSM Param: {param_name}"
                )
                with self.hub_client("ssm") as ssm:
                    try:
                        # todo push into another task
                        ssm.delete_parameter(Name=param_name,)
                        self.info(
                            f"[{self.launch_name}] {self.account_id}:{self.region} :: deleting SSM Param: {param_name}"
                        )
                    except ssm.exceptions.ParameterNotFound:
                        self.info(
                            f"[{self.launch_name}] {self.account_id}:{self.region} :: SSM Param: {param_name} not found"
                        )

            with self.output().open("w") as f:
                f.write(json.dumps(log_output, indent=4, default=str,))

            self.info(
                f"[{self.launch_name}] {self.account_id}:{self.region} :: finished terminating"
            )


class TerminateProductDryRunTask(ProvisioningTask):
    launch_name = luigi.Parameter()
    portfolio = luigi.Parameter()
    portfolio_id = luigi.Parameter()
    product = luigi.Parameter()
    product_id = luigi.Parameter()
    version = luigi.Parameter()
    version_id = luigi.Parameter()

    account_id = luigi.Parameter()
    region = luigi.Parameter()
    puppet_account_id = luigi.Parameter()

    retry_count = luigi.IntParameter(default=1)

    ssm_param_outputs = luigi.ListParameter(default=[])

    worker_timeout = luigi.IntParameter(default=0, significant=False)

    parameters = luigi.ListParameter(default=[])
    ssm_param_inputs = luigi.ListParameter(default=[])

    try_count = 1

    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "launch_name": self.launch_name,
            "account_id": self.account_id,
            "region": self.region,
            "cache_invalidator": self.cache_invalidator,
        }

    def write_result(self, current_version, new_version, effect, notes=""):
        with self.output().open("w") as f:
            f.write(
                json.dumps(
                    {
                        "current_version": current_version,
                        "new_version": new_version,
                        "effect": effect,
                        "notes": notes,
                        "params": self.param_kwargs,
                    },
                    indent=4,
                    default=str,
                )
            )

    def api_calls_used(self):
        return [
            f"servicecatalog.scan_provisioned_products_single_page_{self.account_id}_{self.region}",
            f"servicecatalog.describe_provisioning_artifact_{self.account_id}_{self.region}",
        ]

    def run(self):
        self.info(
            f"starting dry run terminate try {self.try_count} of {self.retry_count}"
        )

        with self.spoke_regional_client("servicecatalog") as service_catalog:
            self.info(
                f"[{self.launch_name}] {self.account_id}:{self.region} :: looking for previous failures"
            )
            r = aws.get_provisioned_product_details(
                self.product_id, self.launch_name, service_catalog
            )

            if r is None:
                self.write_result(
                    "-", "-", constants.NO_CHANGE, notes="There is nothing to terminate"
                )
            else:
                provisioned_product_name = (
                    service_catalog.describe_provisioning_artifact(
                        ProvisioningArtifactId=r.get("ProvisioningArtifactId"),
                        ProductId=self.product_id,
                    )
                    .get("ProvisioningArtifactDetail")
                    .get("Name")
                )

                if r.get("Status") != "TERMINATED":
                    self.write_result(
                        provisioned_product_name,
                        "-",
                        constants.CHANGE,
                        notes="The product would be terminated",
                    )
                else:
                    self.write_result(
                        "-",
                        "-",
                        constants.CHANGE,
                        notes="The product is already terminated",
                    )


class RunDeployInSpokeTask(tasks.PuppetTask):
    manifest_file_path = luigi.Parameter()
    puppet_account_id = luigi.Parameter()
    account_id = luigi.Parameter()

    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "account_id": self.account_id,
            "cache_invalidator": self.cache_invalidator,
        }

    def requires(self):
        return dict(
            shares=generate.GenerateSharesTask(
                puppet_account_id=self.puppet_account_id,
                manifest_file_path=self.manifest_file_path,
                section=constants.LAUNCHES,
            ),
            new_manifest=portfoliomanagement_tasks.GenerateManifestWithIdsTask(
                puppet_account_id=self.puppet_account_id,
                manifest_file_path=self.manifest_file_path,
            ),
        )

    def run(self):
        home_region = config.get_home_region(self.puppet_account_id)
        regions = config.get_regions(self.puppet_account_id)
        should_collect_cloudformation_events = self.should_use_sns
        should_forward_events_to_eventbridge = config.get_should_use_eventbridge(
            self.puppet_account_id
        )
        should_forward_failures_to_opscenter = config.get_should_forward_failures_to_opscenter(
            self.puppet_account_id
        )

        with self.hub_client("s3") as s3:
            bucket = f"sc-puppet-spoke-deploy-{self.puppet_account_id}"
            key = f"{os.getenv('CODEBUILD_BUILD_NUMBER', '0')}.yaml"
            s3.put_object(
                Body=self.input().get("new_manifest").open("r").read(),
                Bucket=bucket,
                Key=key,
            )
            signed_url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=60 * 60 * 24,
            )
        with self.hub_client("ssm") as ssm:
            response = ssm.get_parameter(Name="service-catalog-puppet-version")
            version = response.get("Parameter").get("Value")
        with self.spoke_client("codebuild") as codebuild:
            response = codebuild.start_build(
                projectName=constants.EXECUTION_SPOKE_CODEBUILD_PROJECT_NAME,
                environmentVariablesOverride=[
                    {"name": "VERSION", "value": version, "type": "PLAINTEXT"},
                    {"name": "MANIFEST_URL", "value": signed_url, "type": "PLAINTEXT"},
                    {
                        "name": "PUPPET_ACCOUNT_ID",
                        "value": self.puppet_account_id,
                        "type": "PLAINTEXT",
                    },
                    {"name": "HOME_REGION", "value": home_region, "type": "PLAINTEXT",},
                    {
                        "name": "REGIONS",
                        "value": ",".join(regions),
                        "type": "PLAINTEXT",
                    },
                    {
                        "name": "SHOULD_COLLECT_CLOUDFORMATION_EVENTS",
                        "value": str(should_collect_cloudformation_events),
                        "type": "PLAINTEXT",
                    },
                    {
                        "name": "SHOULD_FORWARD_EVENTS_TO_EVENTBRIDGE",
                        "value": str(should_forward_events_to_eventbridge),
                        "type": "PLAINTEXT",
                    },
                    {
                        "name": "SHOULD_FORWARD_FAILURES_TO_OPSCENTER",
                        "value": str(should_forward_failures_to_opscenter),
                        "type": "PLAINTEXT",
                    },
                ],
            )
        self.write_output(dict(account_id=self.account_id, **response))


class LaunchForSpokeExecutionTask(ProvisioningTask, dependency.DependenciesMixin):
    launch_name = luigi.Parameter()
    puppet_account_id = luigi.Parameter()

    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "launch_name": self.launch_name,
            "cache_invalidator": self.cache_invalidator,
        }

    def requires(self):
        from servicecatalog_puppet.workflow import codebuild_runs
        from servicecatalog_puppet.workflow import spoke_local_portfolios
        from servicecatalog_puppet.workflow import assertions
        from servicecatalog_puppet.workflow import lambda_invocations

        these_dependencies = list()
        common_args = dict(
            manifest_file_path=self.manifest_file_path,
            puppet_account_id=self.puppet_account_id,
        )
        dependencies = self.manifest.get_launch(self.launch_name).get("depends_on", [])
        for depends_on in dependencies:
            depends_on_affinity = depends_on.get(constants.AFFINITY)
            depends_on_type = depends_on.get("type")
            if depends_on_type == constants.LAUNCH:
                if depends_on_affinity == constants.LAUNCH:
                    dep = self.manifest.get_launch(depends_on.get("name"))
                    if dep.get("execution") == constants.EXECUTION_MODE_SPOKE:
                        these_dependencies.append(
                            LaunchForSpokeExecutionTask(
                                **common_args, launch_name=depends_on.get("name"),
                            )
                        )
                    else:
                        these_dependencies.append(
                            LaunchTask(
                                **common_args, launch_name=depends_on.get("name"),
                            )
                        )
                else:
                    raise Exception(
                        "Could can only depend on a launch using affinity launch when using spoke execution mode"
                    )

            elif depends_on_type == constants.SPOKE_LOCAL_PORTFOLIO:
                if depends_on_affinity == constants.SPOKE_LOCAL_PORTFOLIO:
                    these_dependencies.append(
                        spoke_local_portfolios.SpokeLocalPortfolioTask(
                            **common_args,
                            spoke_local_portfolio_name=depends_on.get("name"),
                        )
                    )
                else:
                    raise Exception(
                        "Could can only depend on a spoke_local_portfolio using affinity spoke_local_portfolios when using spoke execution mode"
                    )

            elif depends_on_type == constants.ASSERTION:
                if depends_on_affinity == constants.ASSERTION:
                    these_dependencies.append(
                        assertions.AssertionTask(
                            **common_args, assertion_name=depends_on.get("name"),
                        )
                    )
                else:
                    raise Exception(
                        "Could can only depend on an assertion using affinity assertion when using spoke execution mode"
                    )

            elif depends_on_type == constants.CODE_BUILD_RUN:
                if depends_on_affinity == constants.CODE_BUILD_RUN:
                    these_dependencies.append(
                        codebuild_runs.CodeBuildRunTask(
                            **common_args, code_build_run_name=depends_on.get("name"),
                        )
                    )
                else:
                    raise Exception(
                        "Could can only depend on a code_build_run using affinity code_build_run when using spoke execution mode"
                    )

            elif depends_on_type == constants.LAMBDA_INVOCATION:
                if depends_on_affinity == constants.LAMBDA_INVOCATION:
                    these_dependencies.append(
                        lambda_invocations.LambdaInvocationTask(
                            **common_args,
                            lambda_invocation_name=depends_on.get("name"),
                        )
                    )
                else:
                    raise Exception(
                        "Could can only depend on a lambda_invocation using affinity lambda_invocation when using spoke execution mode"
                    )

        return these_dependencies

    @functools.lru_cache(maxsize=8)
    def get_tasks(self):
        tasks_to_run = list()
        for account_id in self.manifest.get_account_ids_used_for_section_item(
            self.puppet_account_id, self.section_name, self.launch_name
        ):
            tasks_to_run.append(
                RunDeployInSpokeTask(
                    manifest_file_path=self.manifest_file_path,
                    puppet_account_id=self.puppet_account_id,
                    account_id=account_id,
                )
            )
        return tasks_to_run

    def run(self):
        tasks_to_run = self.get_tasks()
        yield tasks_to_run
        self.write_output(self.params_for_results_display())


class LaunchForTask(ProvisioningTask):
    launch_name = luigi.Parameter()
    puppet_account_id = luigi.Parameter()

    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "launch_name": self.launch_name,
            "cache_invalidator": self.cache_invalidator,
        }

    def get_klass_for_provisioning(self):
        if self.is_dry_run:
            if self.status == constants.PROVISIONED:
                return ProvisionProductDryRunTask
            elif self.status == constants.TERMINATED:
                return TerminateProductDryRunTask
            else:
                raise Exception(f"Unknown status: {self.status}")
        else:
            if self.status == constants.PROVISIONED:
                return ProvisionProductTask
            elif self.status == constants.TERMINATED:
                return TerminateProductTask
            else:
                raise Exception(f"Unknown status: {self.status}")

    def run(self):
        self.write_output(self.params_for_results_display())


class LaunchForRegionTask(LaunchForTask):
    region = luigi.Parameter()

    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "launch_name": self.launch_name,
            "region": self.region,
            "cache_invalidator": self.cache_invalidator,
        }

    def requires(self):
        dependencies = list()
        these_dependencies = list()
        requirements = dict(
            dependencies=dependencies, these_dependencies=these_dependencies,
        )

        klass = self.get_klass_for_provisioning()

        for task in self.manifest.get_tasks_for_launch_and_region(
            self.puppet_account_id,
            self.section_name,
            self.launch_name,
            self.region,
            single_account=self.single_account,
        ):
            dependencies.append(
                klass(**task, manifest_file_path=self.manifest_file_path)
            )

        launch = self.manifest.get(self.section_name).get(self.launch_name)
        for depends_on in launch.get("depends_on", []):
            if depends_on.get("type") == self.section_name:
                if depends_on.get(constants.AFFINITY) == "region":
                    these_dependencies.append(
                        self.__class__(
                            manifest_file_path=self.manifest_file_path,
                            launch_name=depends_on.get("name"),
                            puppet_account_id=self.puppet_account_id,
                            region=self.region,
                        )
                    )

        return requirements


class LaunchForAccountTask(LaunchForTask):
    account_id = luigi.Parameter()

    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "launch_name": self.launch_name,
            "account_id": self.account_id,
            "cache_invalidator": self.cache_invalidator,
        }

    def requires(self):
        dependencies = list()
        requirements = dict(dependencies=dependencies,)

        klass = self.get_klass_for_provisioning()

        account_launch_tasks = self.manifest.get_tasks_for_launch_and_account(
            self.puppet_account_id,
            self.section_name,
            self.launch_name,
            self.account_id,
            single_account=self.single_account,
        )
        for task in account_launch_tasks:
            dependencies.append(
                klass(**task, manifest_file_path=self.manifest_file_path)
            )

        return requirements


class LaunchForAccountAndRegionTask(LaunchForTask):
    account_id = luigi.Parameter()
    region = luigi.Parameter()

    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "launch_name": self.launch_name,
            "account_id": self.account_id,
            "region": self.region,
            "cache_invalidator": self.cache_invalidator,
        }

    def requires(self):
        dependencies = list()
        requirements = dict(dependencies=dependencies)

        klass = self.get_klass_for_provisioning()

        for task in self.manifest.get_tasks_for_launch_and_account_and_region(
            self.puppet_account_id,
            self.section_name,
            self.launch_name,
            self.account_id,
            self.region,
            single_account=self.single_account,
        ):
            dependencies.append(
                klass(**task, manifest_file_path=self.manifest_file_path)
            )

        return requirements


class LaunchTask(LaunchForTask):
    def params_for_results_display(self):
        return {
            "puppet_account_id": self.puppet_account_id,
            "launch_name": self.launch_name,
            "cache_invalidator": self.cache_invalidator,
        }

    def requires(self):
        requirements = list()

        klass = self.get_klass_for_provisioning()
        for (
            account_id,
            regions,
        ) in self.manifest.get_account_ids_and_regions_used_for_section_item(
            self.puppet_account_id, constants.LAUNCHES, self.launch_name
        ).items():
            for region in regions:
                for task in self.manifest.get_tasks_for_launch_and_account_and_region(
                    self.puppet_account_id,
                    self.section_name,
                    self.launch_name,
                    account_id,
                    region,
                    single_account=self.single_account,
                ):
                    requirements.append(
                        klass(**task, manifest_file_path=self.manifest_file_path)
                    )

        return requirements

    def run(self):
        self.write_output(self.params_for_results_display())


class ResetProvisionedProductOwnerTask(ProvisioningTask):
    launch_name = luigi.Parameter()
    account_id = luigi.Parameter()
    region = luigi.Parameter()

    def params_for_results_display(self):
        return {
            "launch_name": self.launch_name,
            "account_id": self.account_id,
            "region": self.region,
            "cache_invalidator": self.cache_invalidator,
        }

    def api_calls_used(self):
        return [
            f"servicecatalog.scan_provisioned_products_single_page_{self.account_id}_{self.region}",
            f"servicecatalog.update_provisioned_product_properties_{self.account_id}_{self.region}",
        ]

    def run(self):
        self.info(f"starting ResetProvisionedProductOwnerTask")

        with self.spoke_regional_client("servicecatalog") as service_catalog:
            self.info(f"Checking if existing provisioned product exists")
            all_results = service_catalog.scan_provisioned_products_single_page(
                AccessLevelFilter={"Key": "Account", "Value": "self"},
            ).get("ProvisionedProducts", [])
            changes_made = list()
            for result in all_results:
                if result.get("Name") == self.launch_name:
                    provisioned_product_id = result.get("Id")
                    self.info(f"Ensuring current provisioned product owner is correct")
                    changes_made.append(result)
                    service_catalog.update_provisioned_product_properties(
                        ProvisionedProductId=provisioned_product_id,
                        ProvisionedProductProperties={
                            "OWNER": config.get_puppet_role_arn(self.account_id)
                        },
                    )
            self.write_output(changes_made)
