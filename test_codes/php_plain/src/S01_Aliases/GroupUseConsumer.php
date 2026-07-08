<?php
declare(strict_types=1);

namespace App\S01_Aliases;

use Acme\Reporting\{Report, Exporter as CsvExporter};

class GroupUseConsumer
{
    public function summarize(): string
    {
        $report = new Report();

        return $report->generate();
    }

    public function download(): string
    {
        $exporter = new CsvExporter();

        return $exporter->export();
    }
}
