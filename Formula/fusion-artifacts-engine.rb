class FusionArtifactsEngine < Formula
  desc "Artifact management middleware for the Fusion architecture"
  homepage "https://github.com/dahai80/fusion-artifacts-engine"
  url "https://github.com/dahai80/fusion-artifacts-engine/archive/refs/tags/v0.7.18.tar.gz"
  sha256 "92d068c85282a22ddacaa388a9562a7bb8c8ad6ab8fe3cf3fedfe73e96c15313"
  license "Apache-2.0"

  depends_on "python@3.12"

  include Language::Python::Virtualenv

  def install
    # Personal tap: resolve PyPI dependencies at install time into an isolated
    # Homebrew venv (network installs are accepted in taps, just not in core).
    # Networks behind regional firewalls can export HOMEBREW_PIP_INDEX_URL to
    # point pip at a mirror, e.g. https://pypi.tuna.tsinghua.edu.cn/simple
    ENV["PIP_INDEX_URL"] = ENV.fetch("HOMEBREW_PIP_INDEX_URL", "https://pypi.org/simple")
    system "python3.12", "-m", "venv", libexec
    system libexec/"bin/pip", "install", "."
    bin.install_symlink libexec/"bin/fusion-artifacts-engine"
  end

  def caveats
    <<~EOS
      Configuration and data live under ~/.fusion/artifacts by default.
      Start the daemon with:
        fusion-artifacts-engine start --host 127.0.0.1 --port 11451

      If dependency download fails, retry with a pip mirror:
        HOMEBREW_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple brew install dahai80/fae/fusion-artifacts-engine
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/fusion-artifacts-engine version")
  end
end
