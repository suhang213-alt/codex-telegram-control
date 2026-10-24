using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

public sealed class GatewayConfiguration
{
    public string real_cli { get; set; }
    public string real_cli_sha256 { get; set; }
    public string python { get; set; }
    public string proxy { get; set; }
    public string desktop { get; set; }
    public Dictionary<string, string> sandbox_environment { get; set; }
}

internal static class CodexGatewayShim
{
    // Replaced by installer_plan.py at build time; prevents unnoticed config drift.
    private const string ConfigurationSha256 = "__CONFIGURATION_SHA256__";

    internal static string QuoteArgument(string argument)
    {
        StringBuilder output = new StringBuilder("\"");
        int slashes = 0;
        foreach (char current in argument)
        {
            if (current == '\\') { slashes++; continue; }
            if (current == '"')
            {
                output.Append('\\', slashes * 2 + 1);
                output.Append('"');
                slashes = 0;
                continue;
            }
            output.Append('\\', slashes);
            slashes = 0;
            output.Append(current);
        }
        output.Append('\\', slashes * 2);
        output.Append('"');
        return output.ToString();
    }

    private static string JoinArguments(IEnumerable<string> arguments)
    {
        List<string> quoted = new List<string>();
        foreach (string argument in arguments) quoted.Add(QuoteArgument(argument));
        return String.Join(" ", quoted.ToArray());
    }

    private static string Sha256(string path)
    {
        using (FileStream input = File.OpenRead(path))
        using (SHA256 hash = SHA256.Create())
            return BitConverter.ToString(hash.ComputeHash(input)).Replace("-", "").ToLowerInvariant();
    }

    private static GatewayConfiguration ReadConfiguration()
    {
        string path = Path.Combine(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location), "launcher-config.json");
        if (!String.Equals(Sha256(path), ConfigurationSha256, StringComparison.Ordinal))
            throw new InvalidOperationException("Local gateway configuration identity mismatch");
        GatewayConfiguration config = new JavaScriptSerializer().Deserialize<GatewayConfiguration>(File.ReadAllText(path, Encoding.UTF8));
        foreach (string candidate in new string[] { config.real_cli, config.python, config.proxy })
            if (String.IsNullOrWhiteSpace(candidate) || !Path.IsPathRooted(candidate) || !File.Exists(candidate))
                throw new InvalidOperationException("Local gateway runtime path is unavailable");
        if (!String.Equals(Sha256(config.real_cli), config.real_cli_sha256, StringComparison.Ordinal))
            throw new InvalidOperationException("Original Codex executable identity mismatch");
        if (config.sandbox_environment == null || config.sandbox_environment.Count != 2 ||
            !config.sandbox_environment.ContainsKey("CODEX_WINDOWS_REGISTERED_CORE") ||
            !config.sandbox_environment.ContainsKey("CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY"))
            throw new InvalidOperationException("Exact sandbox environment allowlist required");
        return config;
    }

    internal static bool IsEngineInvocation(string[] arguments)
    {
        bool isAppServer = false;
        HashSet<string> valueOptions = new HashSet<string>(StringComparer.Ordinal) {
            "-c", "--config", "--enable", "--disable", "--listen", "--code-mode-host",
            "--ws-auth", "--ws-token-file", "--ws-token-sha256", "--ws-shared-secret-file",
            "--ws-issuer", "--ws-audience", "--ws-max-clock-skew-seconds", "--profile", "-p"
        };
        for (int index = 0; index < arguments.Length; index++)
        {
            string argument = arguments[index];
            if (argument == "--help" || argument == "-h" || argument == "--version" || argument == "-V") return false;
            if (valueOptions.Contains(argument)) { index++; continue; }
            if (argument.StartsWith("-", StringComparison.Ordinal)) continue;
            if (!isAppServer) {
                if (argument != "app-server") return false;
                isAppServer = true;
            } else {
                // A positional app-server subcommand is CLI tooling, not an engine.
                return false;
            }
        }
        return isAppServer;
    }

    private static void RestoreEngineEnvironment(ProcessStartInfo process, GatewayConfiguration config)
    {
        foreach (KeyValuePair<string, string> entry in config.sandbox_environment)
        {
            if (entry.Value == null) process.EnvironmentVariables.Remove(entry.Key);
            else process.EnvironmentVariables[entry.Key] = entry.Value;
        }
        // Child CLI runtimes must resolve the actual executable, never recurse into this shim.
        process.EnvironmentVariables["CODEX_CLI_PATH"] = config.real_cli;
    }

    private static async Task Pump(Stream input, Stream destination)
    {
        byte[] buffer = new byte[65536];
        int read;
        while ((read = await input.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) != 0)
        {
            await destination.WriteAsync(buffer, 0, read).ConfigureAwait(false);
            // JSON-RPC is interactive: a small complete message must leave the
            // FileStream buffer immediately, without waiting for the next turn or EOF.
            await destination.FlushAsync().ConfigureAwait(false);
        }
    }

    private static async Task CopyInput(Stream input, Stream destination)
    {
        try { await Pump(input, destination).ConfigureAwait(false); }
        catch (IOException) { }
        catch (ObjectDisposedException) { }
        finally { try { destination.Close(); } catch (IOException) { } }
    }

    private static int Run(string[] arguments)
    {
        GatewayConfiguration config = ReadConfiguration();
        bool gateway = IsEngineInvocation(arguments);
        List<string> childArguments = new List<string>();
        if (gateway) childArguments.AddRange(new string[] { "-u", config.proxy, "--real-cli", config.real_cli, "--" });
        childArguments.AddRange(arguments);
        ProcessStartInfo start = new ProcessStartInfo(gateway ? config.python : config.real_cli, JoinArguments(childArguments));
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.RedirectStandardInput = true;
        start.RedirectStandardOutput = true;
        start.RedirectStandardError = true;
        RestoreEngineEnvironment(start, config);
        using (Process child = new Process())
        {
            child.StartInfo = start;
            child.Start();
            // Raw streams: never transcode JSON, normalize line endings, or add stdout diagnostics.
            Task input = CopyInput(Console.OpenStandardInput(), child.StandardInput.BaseStream);
            Task output = Pump(child.StandardOutput.BaseStream, Console.OpenStandardOutput());
            Task error = Pump(child.StandardError.BaseStream, Console.OpenStandardError());
            child.WaitForExit();
            Task.WaitAll(output, error);
            // Do not wait for Desktop stdin EOF after child exit; its pipe may remain open.
            GC.KeepAlive(input);
            return child.ExitCode;
        }
    }

    public static int Main(string[] arguments)
    {
        try { return Run(arguments); }
        catch (Exception error)
        {
            Console.Error.WriteLine("Local Codex gateway refused to start: " + error.Message);
            return 126;
        }
    }
}
