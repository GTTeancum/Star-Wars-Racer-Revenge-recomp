// Headless Ghidra script:
//   1. Exports every function (name, start, end) to build/ghidra/functions.csv
//   2. Decompiles each address given in getScriptArgs() (one file per target
//      under build/ghidra/decomp/<addr>.c)
//
// Run via:
//   analyzeHeadless ghidra_project "Racer Revenge" \
//       -process SLUS_202.68 -noanalysis \
//       -scriptPath tools/ghidra \
//       -postScript DumpState.java 0x2389c0 0x239250 ...
//
// @category PS2Recomp

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.symbol.SourceType;

import java.io.File;
import java.io.PrintWriter;

public class DumpState extends GhidraScript {
    @Override
    public void run() throws Exception {
        File outRoot = new File("build/ghidra");
        File decompDir = new File(outRoot, "decomp");
        decompDir.mkdirs();

        // --- 1. Function CSV (named + default) ---
        File csv = new File(outRoot, "functions.csv");
        PrintWriter w = new PrintWriter(csv);
        w.println("name,start,end,size,is_default,source_type");
        FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
        int total = 0, named = 0;
        while (it.hasNext()) {
            Function f = it.next();
            long start = f.getEntryPoint().getOffset();
            long end = f.getBody().getMaxAddress().getOffset() + 1;
            String name = f.getName();
            SourceType st = f.getSymbol().getSource();
            boolean isDefault = (st == SourceType.DEFAULT) ||
                                name.startsWith("FUN_") || name.startsWith("sub_");
            if (!isDefault) named++;
            w.printf("%s,0x%08x,0x%08x,%d,%s,%s%n",
                     name, start, end, (end - start),
                     isDefault ? "true" : "false", st.name());
            total++;
        }
        w.close();
        println("Wrote " + csv.getAbsolutePath() +
                " (" + total + " functions, " + named + " non-default names)");

        // --- 2. Decompile requested targets ---
        String[] args = getScriptArgs();
        if (args.length == 0) {
            println("No decomp targets supplied. Done.");
            return;
        }

        DecompInterface decomp = new DecompInterface();
        decomp.openProgram(currentProgram);

        for (String arg : args) {
            Address addr;
            try {
                String a = arg.startsWith("0x") ? arg.substring(2) : arg;
                addr = currentProgram.getAddressFactory()
                    .getDefaultAddressSpace().getAddress(Long.parseLong(a, 16));
            } catch (Exception e) {
                println("BAD ADDRESS: " + arg);
                continue;
            }
            Function f = getFunctionAt(addr);
            if (f == null) {
                f = getFunctionContaining(addr);
            }
            if (f == null) {
                println("No function at " + arg);
                continue;
            }
            DecompileResults r = decomp.decompileFunction(f, 60, monitor);
            File out = new File(decompDir,
                String.format("%08x.c", f.getEntryPoint().getOffset()));
            PrintWriter dw = new PrintWriter(out);
            dw.println("// === " + f.getName() + " @ 0x" +
                String.format("%08x", f.getEntryPoint().getOffset()) + " ===");
            dw.println("// Callers:");
            for (Function c : f.getCallingFunctions(monitor)) {
                dw.printf("//   %s @ 0x%08x%n", c.getName(),
                          c.getEntryPoint().getOffset());
            }
            dw.println("// Callees:");
            for (Function c : f.getCalledFunctions(monitor)) {
                dw.printf("//   %s @ 0x%08x%n", c.getName(),
                          c.getEntryPoint().getOffset());
            }
            dw.println();
            if (r != null && r.getDecompiledFunction() != null) {
                dw.println(r.getDecompiledFunction().getC());
            } else {
                dw.println("// decompile failed: " +
                    (r == null ? "null" : r.getErrorMessage()));
            }
            dw.close();
            println("Decompiled " + f.getName() + " -> " + out.getName());
        }
        decomp.dispose();
    }
}
