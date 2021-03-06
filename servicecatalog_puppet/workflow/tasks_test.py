from unittest import skip

from . import tasks_unit_tests_helper


class GetSSMParamTaskTest(tasks_unit_tests_helper.PuppetTaskUnitTest):
    parameter_name = "parameter_name"
    name = "name"
    region = "region"
    depends_on = []
    manifest_file_path = "manifest_file_path"
    puppet_account_id = "puppet_account_id"
    spoke_account_id = "spoke_account_id"
    spoke_region = "spoke_region"

    def setUp(self) -> None:
        from servicecatalog_puppet.workflow import tasks

        self.module = tasks

        self.sut = self.module.GetSSMParamTask(
            parameter_name=self.parameter_name,
            name=self.name,
            region=self.region,
            depends_on=self.depends_on,
            manifest_file_path=self.manifest_file_path,
            puppet_account_id=self.puppet_account_id,
            spoke_account_id=self.spoke_account_id,
            spoke_region=self.spoke_region,
        )

        self.wire_up_mocks()

    def test_params_for_results_display(self):
        # setup
        expected_result = {
            "parameter_name": self.parameter_name,
            "name": self.name,
            "region": self.region,
            "cache_invalidator": self.cache_invalidator,
        }

        # exercise
        actual_result = self.sut.params_for_results_display()

        # verify
        self.assertEqual(expected_result, actual_result)

    def test_no_exception_when_optional_parameters_are_missing(self):
        # setup
        # exercise
        self.module.GetSSMParamTask(
            parameter_name=self.parameter_name, name=self.name, region=self.region,
        )

    def test_api_calls_used(self):
        # setup
        expected_result = ["ssm.get_parameter"]

        # exercise
        actual_result = self.sut.api_calls_used()

        # verify
        self.assertEqual(expected_result, actual_result)

    @skip
    def test_run(self):
        # setup
        # exercise
        actual_result = self.sut.run()

        # verify
        raise NotImplementedError()
