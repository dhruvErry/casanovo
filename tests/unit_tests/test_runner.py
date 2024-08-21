"""Unit tests specifically for the model_runner module."""

from pathlib import Path

import pytest
import torch

from casanovo.config import Config
from casanovo.denovo.model_runner import ModelRunner


def test_initialize_model(tmp_path, mgf_small):
    """Test initializing a new or existing model."""
    config = Config()
    config.model_save_folder_path = tmp_path
    # No model filename given, so train from scratch.
    ModelRunner(config=config).initialize_model(train=True)

    # No model filename given during inference = error.
    with pytest.raises(ValueError):
        ModelRunner(config=config).initialize_model(train=False)

    # Non-existing model filename given during inference = error.
    with pytest.raises(FileNotFoundError):
        runner = ModelRunner(config=config, model_filename="blah")
        runner.initialize_model(train=False)

    # Train a quick model.
    config.max_epochs = 1
    config.n_layers = 1
    ckpt = tmp_path / "existing.ckpt"
    with ModelRunner(config=config) as runner:
        runner.train([mgf_small], [mgf_small])
        runner.trainer.save_checkpoint(ckpt)

    # Resume training from previous model.
    runner = ModelRunner(config=config, model_filename=str(ckpt))
    runner.initialize_model(train=True)

    # Inference with previous model.
    runner = ModelRunner(config=config, model_filename=str(ckpt))
    runner.initialize_model(train=False)

    # If the model initialization throws and EOFError, then the Spec2Pep model
    # has tried to load the weights.
    weights = tmp_path / "blah"
    weights.touch()
    with pytest.raises(EOFError):
        runner = ModelRunner(config=config, model_filename=str(weights))
        runner.initialize_model(train=False)


def test_save_and_load_weights(tmp_path, mgf_small, tiny_config):
    """Test saving and loading weights"""
    config = Config(tiny_config)
    config.max_epochs = 1
    config.n_layers = 1
    ckpt = tmp_path / "test.ckpt"
    mztab = tmp_path / "test.mztab"

    with ModelRunner(config=config) as runner:
        runner.train([mgf_small], [mgf_small])
        runner.trainer.save_checkpoint(ckpt)

    # Try changing model arch:
    other_config = Config(tiny_config)
    other_config.n_layers = 50  # lol
    other_config.n_beams = 12
    other_config.cosine_schedule_period_iters = 2
    with torch.device("meta"):
        # Now load the weights into a new model
        # The device should be meta for all the weights.
        runner = ModelRunner(config=other_config, model_filename=str(ckpt))
        runner.initialize_model(train=False)

    obs_layers = runner.model.encoder.transformer_encoder.num_layers
    assert obs_layers == 1  # Match the original arch.
    assert runner.model.n_beams == 12  # Match the config
    assert runner.model.cosine_schedule_period_iters == 2  # Match the config
    assert next(runner.model.parameters()).device == torch.device("meta")

    # If the Trainer correctly moves the weights to the accelerator,
    # then it should fail if the weights are on the "meta" device.
    with torch.device("meta"):
        with ModelRunner(other_config, model_filename=str(ckpt)) as runner:
            with pytest.raises(NotImplementedError) as err:
                runner.predict([mgf_small], mztab)

    assert "meta tensor; no data!" in str(err.value)

    # Try without arch:
    ckpt_data = torch.load(ckpt)
    del ckpt_data["hyper_parameters"]
    torch.save(ckpt_data, ckpt)

    # Shouldn't work:
    with ModelRunner(other_config, model_filename=str(ckpt)) as runner:
        with pytest.raises(RuntimeError):
            runner.predict([mgf_small], mztab)

    # Should work:
    with ModelRunner(config=config, model_filename=str(ckpt)) as runner:
        runner.predict([mgf_small], mztab)


def test_save_and_load_weights_deprecated(tmp_path, mgf_small, tiny_config):
    """Test saving and loading weights with deprecated config options."""
    config = Config(tiny_config)
    config.max_epochs = 1
    config.cosine_schedule_period_iters = 5
    ckpt = tmp_path / "test.ckpt"

    with ModelRunner(config=config) as runner:
        runner.train([mgf_small], [mgf_small])
        runner.trainer.save_checkpoint(ckpt)

    # Replace the new config option with the deprecated one.
    ckpt_data = torch.load(ckpt)
    ckpt_data["hyper_parameters"]["max_iters"] = 5
    del ckpt_data["hyper_parameters"]["cosine_schedule_period_iters"]
    torch.save(ckpt_data, str(ckpt))

    # Inference.
    with ModelRunner(config=config, model_filename=str(ckpt)) as runner:
        runner.initialize_model(train=False)
        assert runner.model.cosine_schedule_period_iters == 5
    # Fine-tuning.
    with ModelRunner(config=config, model_filename=str(ckpt)) as runner:
        with pytest.warns(DeprecationWarning):
            runner.train([mgf_small], [mgf_small])
            assert "max_iters" not in runner.model.opt_kwargs


def test_calculate_precision(tmp_path, mgf_small, tiny_config):
    """Test that this parameter is working correctly."""
    config = Config(tiny_config)
    config.n_layers = 1
    config.max_epochs = 1
    config.calculate_precision = False
    config.tb_summarywriter = str(tmp_path)

    runner = ModelRunner(config=config)
    with runner:
        runner.train([mgf_small], [mgf_small])

    assert "valid_aa_precision" not in runner.model.history.columns
    assert "valid_pep_precision" not in runner.model.history.columns

    config.calculate_precision = True
    runner = ModelRunner(config=config)
    with runner:
        runner.train([mgf_small], [mgf_small])

    assert "valid_aa_precision" in runner.model.history.columns
    assert "valid_pep_precision" in runner.model.history.columns


def test_save_final_model(tmp_path, mgf_small, tiny_config):
    """Test that final model checkpoints are saved."""
    # Test checkpoint saving when val_check_interval is greater than training steps
    config = Config(tiny_config)
    config.val_check_interval = 50
    model_file = tmp_path / "epoch=19-step=20.ckpt"
    with ModelRunner(config) as runner:
        runner.train([mgf_small], [mgf_small])

    assert model_file.exists()
    Path.unlink(model_file)

    # Test checkpoint saving when val_check_interval is not a factor of training steps
    config.val_check_interval = 15
    validation_file = tmp_path / "foobar.best.ckpt"
    with ModelRunner(config, output_rootname="foobar") as runner:
        runner.train([mgf_small], [mgf_small])

    assert model_file.exists()
    assert validation_file.exists()


def test_evaluate(
    tmp_path, mgf_small, mzml_small, mgf_small_unannotated, tiny_config
):
    """Test model evaluation during sequencing"""
    # Train tiny model
    config = Config(tiny_config)
    config.max_epochs = 1
    model_file = tmp_path / "epoch=0-step=1.ckpt"
    with ModelRunner(config) as runner:
        runner.train([mgf_small], [mgf_small])

    assert model_file.is_file()

    # Test evaluation with annotated peak file
    result_file = tmp_path / "result.mztab"
    with ModelRunner(config, model_filename=str(model_file)) as runner:
        runner.predict([mgf_small], result_file, evaluate=True)

    assert result_file.is_file()
    result_file.unlink()

    exception_string = (
        "Error creating annotated spectrum index. "
        "This may be the result of having an unannotated MGF file "
        "present in the validation peak file path list.\n"
    )

    with pytest.raises(FileNotFoundError):
        with ModelRunner(config, model_filename=str(model_file)) as runner:
            runner.predict([mzml_small], result_file, evaluate=True)

    with pytest.raises(TypeError, match=exception_string):
        with ModelRunner(config, model_filename=str(model_file)) as runner:
            runner.predict([mgf_small_unannotated], result_file, evaluate=True)

    with pytest.raises(TypeError, match=exception_string):
        with ModelRunner(config, model_filename=str(model_file)) as runner:
            runner.predict(
                [mgf_small_unannotated, mzml_small], result_file, evaluate=True
            )

    # MzTab with just metadata is written in the case of FileNotFound
    # or TypeError early exit
    assert result_file.is_file()
    result_file.unlink()

    # Test mix of annotated an unannotated peak files
    with pytest.warns(RuntimeWarning):
        with ModelRunner(config, model_filename=str(model_file)) as runner:
            runner.predict([mgf_small, mzml_small], result_file, evaluate=True)

    assert result_file.is_file()
    result_file.unlink()

    with pytest.raises(TypeError, match=exception_string):
        with ModelRunner(config, model_filename=str(model_file)) as runner:
            runner.predict(
                [mgf_small, mgf_small_unannotated], result_file, evaluate=True
            )

    assert result_file.is_file()
    result_file.unlink()

    with pytest.raises(TypeError, match=exception_string):
        with ModelRunner(config, model_filename=str(model_file)) as runner:
            runner.predict(
                [mgf_small, mgf_small_unannotated, mzml_small],
                result_file,
                evaluate=True,
            )

    result_file.unlink()
