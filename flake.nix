{
  description = "Diet CLI - nutrition tracker powered by Claude Agent SDK";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
  };

  outputs = { self, nixpkgs, ... }:
  let
    system = "x86_64-linux";
    pkgs = nixpkgs.legacyPackages.${system};

    # Points to live source so edits take effect immediately (no rebuild needed)
    dietDir = "/home/nicholas/git/mine/diet";
    venvPython = "/var/lib/claude-sdk/venv/bin/python";
  in
  {
    packages.${system} = {
      default = pkgs.writeShellScriptBin "diet" ''
        if [ ! -f "${venvPython}" ]; then
          echo "Error: Claude SDK venv not found at ${venvPython}" >&2
          echo "Run: sudo nixos-rebuild switch --flake /etc/nixos#cmos" >&2
          exit 1
        fi
        exec "${venvPython}" "${dietDir}/diet.py" "$@"
      '';

      init-db = pkgs.writeShellScriptBin "diet-init-db" ''
        exec "${venvPython}" "${dietDir}/init_db.py" "$@"
      '';
    };
  };
}
