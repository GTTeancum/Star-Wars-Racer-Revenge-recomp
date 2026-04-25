// @category Analysis
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.listing.*;
public class Decomp308c08 extends GhidraScript {
    @Override
    public void run() throws Exception {
        DecompInterface decomp = new DecompInterface();
        decomp.openProgram(currentProgram);
        long[] targets = {0x308C08, 0x308C28, 0x2F6370, 0x2F6378, 0x2FEA30, 0x302DF0};
        for (long addr : targets) {
            Function fn = getFunctionAt(toAddr(addr));
            if (fn == null) { println("No func at 0x" + Long.toHexString(addr)); continue; }
            println("\n=== " + fn.getName() + " @ 0x" + Long.toHexString(addr) + " ===");
            DecompileResults r = decomp.decompileFunction(fn, 60, null);
            if (r != null && r.decompileCompleted()) {
                ClangTokenGroup t = r.getCCodeMarkup();
                if (t != null) println(t.toString());
            }
        }
        decomp.dispose();
    }
}
