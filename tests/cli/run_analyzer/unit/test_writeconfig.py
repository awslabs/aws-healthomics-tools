import os
import tempfile
import textwrap

from omics.cli.run_analyzer.writeconfig import create_config


def test_create_config():
    """Test the create_config function"""
    # Test data
    task_resources = {"task1": {"cpus": 2, "mem": "4"}, "task2": {"cpus": 4, "mem": "8"}}

    # Test NEXTFLOW config creation
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        create_config("NEXTFLOW", task_resources, tmp.name)

        with open(tmp.name) as f:
            content = f.read()

        # Clean up
        os.unlink(tmp.name)

        # Verify content
        expected = textwrap.dedent("""process {
withName: task1 {
    cpus = 2
    memory = 4.GB
}

withName: task2 {
    cpus = 4
    memory = 8.GB
}
}""")
    assert content.strip() == expected.strip()

    # Test invalid engine
    try:
        create_config("INVALID", task_resources, "test.config")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    # Test CWL
    try:
        create_config("CWL", task_resources, "test.config")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    # Test WDL
    try:
        create_config("WDL", task_resources, "test.config")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
