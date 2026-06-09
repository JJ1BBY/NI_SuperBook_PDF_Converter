#pragma warning disable CA2235 // Mark all non-serializable fields

using System;
using System.Buffers;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.CompilerServices;
using System.Diagnostics.CodeAnalysis;
using System.Runtime.Serialization;

using IPA.Cores.Basic;
using IPA.Cores.Helper.Basic;
using static IPA.Cores.Globals.Basic;

using IPA.Cores.Codes;
using IPA.Cores.Helper.Codes;
using static IPA.Cores.Globals.Codes;

using SuperBookTools;
using SuperBookTools.App;

namespace SuperBookTools.App
{
    public static class MainClass
    {
        static int Main(string[] args)
        {
            int ret = -1;

            CoresLib.Init(new CoresLibOptions(CoresMode.Application, "SuperBookTools", DebugMode.Debug, defaultPrintStatToConsole: false, defaultRecordLeakFullStack: false), args);

            try
            {
                ret = ConsoleService.EntryPoint(Env.CommandLine, "SuperBookTools", typeof(MainClass));
            }
            finally
            {
                CoresLib.Free();
            }

            return ret;
        }

        [ConsoleCommand(
            "SuperBookTools App Program",
            "[/IN:infile] [/OUT:outfile] [/CSV] [/CMD command_line...]",
            "This is the SuperBookTools App Program.",
            "IN:This will specify the text file 'infile' that contains the list of commands that are automatically executed after the connection is completed. If the /IN parameter is specified, this program will terminate automatically after the execution of all commands in the file are finished. If the file contains multiple-byte characters, the encoding must be Unicode (UTF-8). This cannot be specified together with /CMD (if /CMD is specified, /IN will be ignored).",
            "OUT:If the optional command 'commands...' is included after /CMD, that command will be executed after the connection is complete and this program will terminate after that. This cannot be specified together with /IN (if specified together with /IN, /IN will be ignored). Specify the /CMD parameter after all other parameters.",
            "CMD:If the optional command 'commands...' is included after /CMD, that command will be executed after the connection is complete and this program will terminate after that. This cannot be specified together with /IN (if specified together with /IN, /IN will be ignored). Specify the /CMD parameter after all other parameters.",
            "CSV:You can specify this option to enable CSV outputs. Results of each command will be printed in the CSV format. It is useful for processing the results by other programs."
            )]
        static int SuperBookTools(ConsoleService c, string cmdName, string str)
        {
            c.WriteLine($"Copyright (c) 2025-{DateTimeOffset.Now.Year} Daiyuu Nobori. All Rights Reserved.");
            c.WriteLine("");

            ConsoleParam[] args =
            {
                new ConsoleParam("IN", null, null, null, null),
                new ConsoleParam("OUT", null, null, null, null),
                new ConsoleParam("CMD", null, null, null, null),
                new ConsoleParam("CSV", null, null, null, null),
            };

            ConsoleParamValueList vl = c.ParseCommandList(cmdName, str, args, noErrorOnUnknownArg: true);

            string cmdline = vl["CMD"].StrValue;

            ConsoleService cs = c;

            while (cs.DispatchCommand(cmdline, "SuperBookTools>", typeof(Commands), null))
            {
                if (Str.IsEmptyStr(cmdline) == false)
                {
                    break;
                }
            }

            return cs.RetCode;
        }
    }
}
