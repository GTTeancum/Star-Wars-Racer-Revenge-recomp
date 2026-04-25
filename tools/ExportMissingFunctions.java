// Export functions in the 0x2FD000-0x2FE000 gap to CSV for the recompiler
// @category Analysis
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.*;
import java.io.*;

public class ExportMissingFunctions extends GhidraScript {
    @Override
    public void run() throws Exception {
        String outPath = "C:/Programming/GitHub/Racer Revenge decomp/config/ghidra_functions.csv";
        PrintWriter pw = new PrintWriter(new FileWriter(outPath));
        pw.println("Name,Start,End,Size");

        int count = 0;
        FunctionIterator iter = currentProgram.getListing().getFunctions(toAddr(0x100000), true);
        while (iter.hasNext()) {
            Function fn = iter.next();
            long start = fn.getEntryPoint().getOffset();
            if (start > 0x44F600) break;

            // Get the function's max address
            long end = fn.getBody().getMaxAddress().getOffset() + 1;

            // Export ALL functions — the recompiler will merge with its own analysis
            pw.printf("%s,0x%08X,0x%08X,%d%n",
                fn.getName(), start, end, end - start);
            count++;
        }

        pw.close();
        println("Exported " + count + " functions to " + outPath);
    }
}
